// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {SarfDelegate} from "../src/SarfDelegate.sol";

interface IUniV2Router {
    function swapExactTokensForTokensSupportingFeeOnTransferTokens(
        uint256 amountIn, uint256 amountOutMin, address[] calldata path, address to, uint256 deadline
    ) external;
}

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function decimals() external view returns (uint8);
}

/**
 * @dev Against a fork of X Layer mainnet, with the real router, the real pool
 *      and the real RIP-7212 precompile. Two things can only be tested here:
 *
 *        - the passkey path, because P256VERIFY at 0x100 is a property of the
 *          chain. Foundry's local EVM does not implement it, so a unit test of
 *          it would prove only that a mock agrees with itself.
 *        - the hard rule against a real router, whose `to` parameter is a real
 *          drain and not a mock's approximation of one.
 *
 *      Skips (rather than fails) when there is no RPC, so the suite still runs
 *      offline. Run with: forge test --match-path test/SarfDelegateFork.t.sol
 */
contract SarfDelegateForkTest is Test {
    // Verified against X Layer RPC on 2026-09-29; same values as
    // server/sarf/xlayer/zap_pools.json.
    address constant ROUTER = 0x182a927119D56008d921126764bF884221b10f59;
    address constant PAIR_LAIKA_WSPCXX = 0x68084c1d2873b22F4Ef2DcE9C293EF2C12576147;
    address constant WSPCXX = 0x8e2eeD8b8B5E13Ea7BF38e50d7821d2C57309072;
    address constant LAIKA = 0x4fEC966f98D8530507787d947D1ab24Fa145a999;

    uint256 userPk = 0xA11CE;
    address user;
    address keeper = address(0xC0FFEE);
    address attacker = address(0xBAD);

    SarfDelegate impl;
    SarfDelegate acct;
    bool forked;

    function setUp() public {
        try vm.createSelectFork("https://rpc.xlayer.tech") {
            forked = true;
        } catch {
            return; // no network: every test below no-ops
        }
        impl = new SarfDelegate();
        user = vm.addr(userPk);
        vm.signAndAttachDelegation(address(impl), userPk);
        acct = SarfDelegate(payable(user));

        address[] memory targets = new address[](1);
        targets[0] = ROUTER;
        address[] memory tokens = new address[](1);
        tokens[0] = WSPCXX;
        uint128[] memory pa = new uint128[](1);
        pa[0] = 1e18;
        uint128[] memory dy = new uint128[](1);
        dy[0] = 3e18;
        vm.prank(user);
        acct.setScope(SarfDelegate.ScopeSpec({
            keeper: keeper, expiry: uint64(block.timestamp + 7 days),
            targets: targets, tokens: tokens, perAction: pa, daily: dy
        }));
        deal(WSPCXX, user, 10e18);
    }

    function _swapCall(uint256 amtIn, address to) internal view returns (bytes memory) {
        address[] memory path = new address[](2);
        path[0] = WSPCXX;
        path[1] = LAIKA;
        return abi.encodeCall(
            IUniV2Router.swapExactTokensForTokensSupportingFeeOnTransferTokens,
            (amtIn, 0, path, to, block.timestamp + 600)
        );
    }

    function _legs(uint256 amt) internal pure returns (SarfDelegate.Leg[] memory l) {
        l = new SarfDelegate.Leg[](1);
        l[0] = SarfDelegate.Leg({token: WSPCXX, amount: amt, spender: address(0)});
    }

    function _wants(uint256 minOut) internal pure returns (SarfDelegate.Want[] memory w) {
        w = new SarfDelegate.Want[](1);
        w[0] = SarfDelegate.Want({token: LAIKA, minOut: minOut});
    }

    function test_real_router_action_lands_in_the_users_own_account() public {
        if (!forked) return;
        uint256 before = IERC20(LAIKA).balanceOf(user);

        vm.prank(keeper);
        acct.execute(ROUTER, _swapCall(1e18, user), _legs(1e18), _wants(1));

        assertGt(IERC20(LAIKA).balanceOf(user), before, "proceeds arrived at the user's own EOA");
        assertEq(IERC20(WSPCXX).balanceOf(user), 9e18, "exactly the declared amount left");
        assertEq(acct.remainingToday(WSPCXX), 2e18);
    }

    /// The drain, through a real router whose `to` really does pay whoever it
    /// is told to. This is the test the whole design exists for.
    function test_real_router_drain_to_attacker_reverts() public {
        if (!forked) return;
        vm.prank(keeper);
        vm.expectRevert(SarfDelegate.ReceivedTooLittle.selector);
        acct.execute(ROUTER, _swapCall(1e18, attacker), _legs(1e18), _wants(1));

        assertEq(IERC20(LAIKA).balanceOf(attacker), 0);
        assertEq(IERC20(WSPCXX).balanceOf(user), 10e18, "nothing left the account");
    }

    function test_real_delegation_shows_the_7702_prefix() public {
        if (!forked) return;
        bytes memory code = user.code;
        assertEq(code.length, 23, "7702 delegation indicator is 23 bytes");
        assertEq(uint8(code[0]), 0xef);
        assertEq(uint8(code[1]), 0x01);
        assertEq(uint8(code[2]), 0x00);
        address pointsAt;
        assembly { pointsAt := shr(96, mload(add(code, 35))) }
        assertEq(pointsAt, address(impl), "and it points at this implementation");
    }

    // ------------------------------------------------- passkey, on real chain
    //
    // Split in two on purpose. Foundry's EVM does not implement RIP-7212 even
    // when forking (forking fetches state; execution stays local), so there is
    // no way to run a real P-256 signature through the real precompile inside
    // the contract here. Rather than pretend otherwise:
    //
    //   - the precompile itself is proven against the live chain over RPC,
    //     with a signature generated in this test;
    //   - the contract's own logic around it (challenge binding, nonce,
    //     low-s, no-passkey) is tested with 0x100 mocked.
    //
    // The one thing neither covers is the two meeting on-chain. That needs a
    // testnet deployment and is listed as such in the audit notes.

    /// The chain-level claim, checked against the real node, not a mock.
    function test_p256_precompile_verifies_on_the_live_chain() public {
        if (!forked) return;
        uint256 pk = 0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef;
        (uint256 px, uint256 py) = vm.publicKeyP256(pk);
        bytes32 h = sha256("sarf p256 probe");
        (bytes32 rr, bytes32 ss) = vm.signP256(pk, h);
        uint256 s = uint256(ss);
        uint256 n = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551;
        if (s > n / 2) s = n - s;

        bytes memory good = abi.encode(h, uint256(rr), s, px, py);
        bytes memory params = abi.encodePacked(
            '[{"to":"0x0000000000000000000000000000000000000100","data":"',
            vm.toString(good), '"},"latest"]'
        );
        bytes memory out = vm.rpc("eth_call", string(params));
        assertEq(abi.decode(out, (uint256)), 1, "X Layer verifies a valid P-256 signature");

        // and refuses a bad one: an empty return, which is also what a chain
        // without the precompile gives, so the contract fails closed either way.
        bytes memory bad = abi.encode(sha256("a different message"), uint256(rr), s, px, py);
        bytes memory params2 = abi.encodePacked(
            '[{"to":"0x0000000000000000000000000000000000000100","data":"',
            vm.toString(bad), '"},"latest"]'
        );
        assertEq(vm.rpc("eth_call", string(params2)).length, 0, "invalid signature returns empty");
    }


    function test_passkey_sets_a_scope_with_the_precompile_mocked() public {
        if (!forked) return;
        uint256 pk = 0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef;
        (uint256 px, uint256 py) = vm.publicKeyP256(pk);
        vm.prank(user);
        acct.registerPasskey(px, py);

        SarfDelegate.ScopeSpec memory spec = _spec(keeper, uint64(block.timestamp + 3 days), 5e17);
        uint256 nonce = 7;
        _mockP256Valid(_assertion(pk, _digest(spec, nonce)));
        acct.setScopeWithPasskey(spec, nonce, _assertion(pk, _digest(spec, nonce)));

        (address k,) = acct.scope();
        assertEq(k, keeper);
        assertEq(acct.remainingToday(WSPCXX), 5e17, "the passkey's caps replaced the old ones");
    }

    function test_passkey_replay_and_forgery_are_refused() public {
        if (!forked) return;
        uint256 pk = 0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef;
        (uint256 px, uint256 py) = vm.publicKeyP256(pk);
        vm.prank(user);
        acct.registerPasskey(px, py);

        SarfDelegate.ScopeSpec memory spec = _spec(keeper, uint64(block.timestamp + 3 days), 5e17);
        _mockP256Valid(_assertion(pk, _digest(spec, 7)));
        acct.setScopeWithPasskey(spec, 7, _assertion(pk, _digest(spec, 7)));

        SarfDelegate.PasskeySig memory again = _assertion(pk, _digest(spec, 7));
        vm.expectRevert(SarfDelegate.PasskeyNonceUsed.selector);
        acct.setScopeWithPasskey(spec, 7, again);

        // A signature from a key that is not the registered one: the
        // precompile says no (empty return), and so does the contract.
        SarfDelegate.PasskeySig memory forged = _assertion(uint256(0xBADBEEF), _digest(spec, 8));
        vm.mockCall(address(0x100), _p256Input(forged, _digest(spec, 8)), "");
        vm.expectRevert(SarfDelegate.BadPasskeySignature.selector);
        acct.setScopeWithPasskey(spec, 8, forged);
    }

    /// A real assertion the user made for something else must not be reusable
    /// to hand an attacker a scope.
    function test_passkey_assertion_for_another_challenge_is_refused() public {
        if (!forked) return;
        uint256 pk = 0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef;
        (uint256 px, uint256 py) = vm.publicKeyP256(pk);
        vm.prank(user);
        acct.registerPasskey(px, py);

        SarfDelegate.ScopeSpec memory evil = _spec(attacker, uint64(block.timestamp + 3 days), 1e18);
        SarfDelegate.PasskeySig memory elsewhere =
            _assertion(pk, keccak256("a login the user signed on some other site"));
        vm.expectRevert(SarfDelegate.ChallengeMismatch.selector);
        acct.setScopeWithPasskey(evil, 9, elsewhere);
    }

    function test_passkey_path_is_closed_until_one_is_registered() public {
        if (!forked) return;
        SarfDelegate.ScopeSpec memory spec = _spec(keeper, uint64(block.timestamp + 1 days), 1e18);
        SarfDelegate.PasskeySig memory sig = SarfDelegate.PasskeySig({
            authenticatorData: hex"00", clientDataJSON: "{}", challengeIndex: 0, r: 1, s: 1
        });
        vm.expectRevert(SarfDelegate.NoPasskey.selector);
        acct.setScopeWithPasskey(spec, 1, sig);
    }

    // -------------------------------------------------------------- helpers

    /// @dev Stand in for RIP-7212 while running on Foundry's EVM. Mocks only
    ///      the exact input the contract will build for THIS assertion, so a
    ///      test that gets the digest or the challenge wrong still fails.
    function _mockP256Valid(SarfDelegate.PasskeySig memory sig) internal {
        bytes32 msgHash = sha256(abi.encodePacked(
            sig.authenticatorData, sha256(bytes(sig.clientDataJSON))));
        (uint256 px, uint256 py) = (acct.passkeyX(), acct.passkeyY());
        vm.mockCall(
            address(0x100),
            abi.encode(msgHash, sig.r, sig.s, px, py),
            abi.encode(uint256(1))
        );
    }

    function _p256Input(SarfDelegate.PasskeySig memory sig, bytes32) internal view returns (bytes memory) {
        bytes32 msgHash = sha256(abi.encodePacked(
            sig.authenticatorData, sha256(bytes(sig.clientDataJSON))));
        return abi.encode(msgHash, sig.r, sig.s, acct.passkeyX(), acct.passkeyY());
    }

    function _spec(address k, uint64 expiry, uint128 cap)
        internal pure returns (SarfDelegate.ScopeSpec memory spec)
    {
        address[] memory targets = new address[](1);
        targets[0] = ROUTER;
        address[] memory tokens = new address[](1);
        tokens[0] = WSPCXX;
        uint128[] memory pa = new uint128[](1);
        pa[0] = cap;
        uint128[] memory dy = new uint128[](1);
        dy[0] = cap;
        spec = SarfDelegate.ScopeSpec({
            keeper: k, expiry: expiry, targets: targets, tokens: tokens, perAction: pa, daily: dy
        });
    }

    function _digest(SarfDelegate.ScopeSpec memory spec, uint256 nonce)
        internal view returns (bytes32)
    {
        return keccak256(abi.encode(
            user, block.chainid, spec.keeper, spec.expiry,
            keccak256(abi.encodePacked(spec.targets)), keccak256(abi.encodePacked(spec.tokens)),
            keccak256(abi.encodePacked(spec.perAction)), keccak256(abi.encodePacked(spec.daily)),
            nonce
        ));
    }

    /// @dev A real WebAuthn assertion: the browser signs
    ///      sha256(authData ‖ sha256(clientDataJSON)), never the digest itself,
    ///      and the challenge inside clientDataJSON is unpadded base64url.
    function _assertion(uint256 pk, bytes32 digest)
        internal pure returns (SarfDelegate.PasskeySig memory)
    {
        bytes memory authData =
            hex"49960de5880e8c687434170f6476605b8fe4aeb9a28632c7995cf3ba831d976304000000ff";
        bytes memory padded = bytes(vm.toBase64URL(abi.encodePacked(digest)));
        bytes memory challenge = new bytes(43);
        for (uint256 i; i < 43; ++i) challenge[i] = padded[i];
        string memory clientData = string(abi.encodePacked(
            '{"type":"webauthn.get","challenge":"',
            challenge,
            '","origin":"https://getsarf.xyz","crossOrigin":false}'
        ));
        bytes32 msgHash = sha256(abi.encodePacked(authData, sha256(bytes(clientData))));
        (bytes32 rr, bytes32 ss) = vm.signP256(pk, msgHash);
        uint256 s = uint256(ss);
        uint256 n = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551;
        if (s > n / 2) s = n - s; // low-s, as the contract requires
        return SarfDelegate.PasskeySig({
            authenticatorData: authData,
            clientDataJSON: clientData,
            challengeIndex: 23,
            r: uint256(rr),
            s: s
        });
    }
}

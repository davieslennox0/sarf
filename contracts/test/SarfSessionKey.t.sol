// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {SarfSessionKey} from "../src/SarfSessionKey.sol";

/// @dev USDT-style: `approve` returns nothing. The stable leg of every trade
///      here behaves this way, so testing against a well-behaved token only
///      would test the wrong thing.
contract MockToken {
    string public symbol;
    uint8 public immutable decimals;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    bool public immutable silentApprove;

    constructor(string memory s, uint8 d, bool silent) {
        symbol = s;
        decimals = d;
        silentApprove = silent;
    }

    function mint(address to, uint256 v) external { balanceOf[to] += v; }

    function approve(address sp, uint256 v) external returns (bool) {
        allowance[msg.sender][sp] = v;
        if (silentApprove) assembly { return(0, 0) }
        return true;
    }

    function transferFrom(address f, address t, uint256 v) external returns (bool) {
        require(allowance[f][msg.sender] >= v, "allowance");
        allowance[f][msg.sender] -= v;
        require(balanceOf[f] >= v, "balance");
        balanceOf[f] -= v;
        balanceOf[t] += v;
        return true;
    }

    function transfer(address t, uint256 v) external returns (bool) {
        require(balanceOf[msg.sender] >= v, "balance");
        balanceOf[msg.sender] -= v;
        balanceOf[t] += v;
        return true;
    }
}

/// @dev Stands in for the OKX aggregator: pulls the sell token, pays the buy
///      token at a fixed rate. `rateBps` lets a test make it pay badly.
contract MockRouter {
    uint256 public rateBps = 10_000;
    function setRate(uint256 b) external { rateBps = b; }

    function swap(address sell, address buy, uint256 amountIn, uint256 out) external {
        MockToken(sell).transferFrom(msg.sender, address(this), amountIn);
        MockToken(buy).mint(msg.sender, (out * rateBps) / 10_000);
    }

    /// Takes the allowance and gives nothing back.
    function rug(address sell, uint256 amountIn) external {
        MockToken(sell).transferFrom(msg.sender, address(this), amountIn);
    }

    /// Tries to drain far beyond what was authorised for this trade.
    function overdraw(address sell, uint256 amountIn) external {
        MockToken(sell).transferFrom(msg.sender, address(this), amountIn);
    }
}

contract SarfSessionKeyTest is Test {
    SarfSessionKey impl;
    MockToken usdt;
    MockToken aapl;
    MockRouter router;

    // The user's EOA, delegated to `impl` via EIP-7702 in setUp.
    uint256 userPk = 0xA11CE;
    address user;
    // The key Sarf holds. Never has authority of its own.
    uint256 sessionPk = 0x5E551;
    address sessionKey;

    uint128 constant PER_TRADE = 500e6;   // $500
    uint128 constant DAILY = 2_000e6;     // $2,000

    function setUp() public {
        user = vm.addr(userPk);
        sessionKey = vm.addr(sessionPk);
        impl = new SarfSessionKey();
        usdt = new MockToken("USDT", 6, true);   // silent approve, like real USDT
        aapl = new MockToken("AAPLx", 18, false);
        router = new MockRouter();

        // EIP-7702: point the EOA at the implementation. This is the exact
        // mechanism the contract relies on, so the tests use it rather than
        // testing the implementation in isolation.
        vm.signAndAttachDelegation(address(impl), userPk);

        usdt.mint(user, 10_000e6);
        aapl.mint(user, 100e18);

        address[] memory toks = new address[](1);
        toks[0] = address(aapl);
        vm.prank(user);
        SarfSessionKey(payable(user)).authorize(
            sessionKey, uint64(block.timestamp + 7 days), address(router),
            address(usdt), PER_TRADE, DAILY, toks
        );
    }

    // ------------------------------------------------------------- helpers

    function _sign(
        address sellToken, address buyToken, uint256 sellAmount, uint256 minBuy,
        bytes memory data, uint256 nonce, uint256 pk
    ) internal view returns (bytes memory) {
        bytes32 inner = keccak256(abi.encode(
            keccak256(
                "SarfSwap(address account,uint256 chainId,address sellToken,address buyToken,"
                "uint256 sellAmount,uint256 minBuyAmount,address target,bytes32 dataHash,"
                "uint256 nonce,uint256 deadline)"
            ),
            user, block.chainid, sellToken, buyToken, sellAmount, minBuy,
            address(router), keccak256(data), nonce, block.timestamp + 300
        ));
        bytes32 digest = keccak256(abi.encodePacked("\x19Ethereum Signed Message:\n32", inner));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        return abi.encodePacked(r, s, v);
    }

    function _buy(uint256 usdtIn, uint256 minOut, uint256 nonce) internal returns (uint256) {
        bytes memory data = abi.encodeCall(
            MockRouter.swap, (address(usdt), address(aapl), usdtIn, minOut)
        );
        bytes memory sig = _sign(address(usdt), address(aapl), usdtIn, minOut, data, nonce, sessionPk);
        return SarfSessionKey(payable(user)).executeSwap(
            address(usdt), address(aapl), usdtIn, minOut, address(router),
            data, nonce, block.timestamp + 300, sig
        );
    }

    // ---------------------------------------------------------- happy path

    function test_session_key_executes_a_swap_without_the_user() public {
        uint256 bought = _buy(100e6, 0.3e18, 1);
        assertEq(bought, 0.3e18);
        assertEq(usdt.balanceOf(user), 9_900e6);
        assertEq(aapl.balanceOf(user), 100.3e18);
    }

    function test_user_wallet_key_is_never_needed_to_trade() public {
        // Nothing is pranked as `user`; the signature is the whole authority.
        _buy(100e6, 0.3e18, 1);
        assertEq(aapl.balanceOf(user), 100.3e18);
    }

    function test_anyone_may_relay_the_signed_swap() public {
        vm.prank(address(0xBEEF));
        _buy(100e6, 0.3e18, 1);
        assertEq(aapl.balanceOf(user), 100.3e18);
    }

    // ------------------------------------------------- the post-conditions
    // These are the tests the whole design exists for.

    function test_router_that_takes_the_money_and_returns_nothing_reverts() public {
        bytes memory data = abi.encodeCall(MockRouter.rug, (address(usdt), 100e6));
        bytes memory sig = _sign(address(usdt), address(aapl), 100e6, 0.3e18, data, 1, sessionPk);
        vm.expectRevert(SarfSessionKey.ReceivedTooLittle.selector);
        SarfSessionKey(payable(user)).executeSwap(
            address(usdt), address(aapl), 100e6, 0.3e18, address(router),
            data, 1, block.timestamp + 300, sig
        );
        assertEq(usdt.balanceOf(user), 10_000e6, "funds must be untouched after revert");
    }

    function test_router_cannot_pull_more_than_the_authorised_amount() public {
        // Ask to spend 100 but hand the router calldata that pulls 5,000.
        bytes memory data = abi.encodeCall(MockRouter.overdraw, (address(usdt), 5_000e6));
        bytes memory sig = _sign(address(usdt), address(aapl), 100e6, 0, data, 1, sessionPk);
        vm.expectRevert();  // the exact allowance (100e6) starves the pull
        SarfSessionKey(payable(user)).executeSwap(
            address(usdt), address(aapl), 100e6, 0, address(router),
            data, 1, block.timestamp + 300, sig
        );
        assertEq(usdt.balanceOf(user), 10_000e6);
    }

    function test_bad_price_reverts_on_min_received() public {
        router.setRate(5_000); // pays half
        bytes memory data = abi.encodeCall(
            MockRouter.swap, (address(usdt), address(aapl), 100e6, 0.3e18)
        );
        bytes memory sig = _sign(address(usdt), address(aapl), 100e6, 0.3e18, data, 1, sessionPk);
        vm.expectRevert(SarfSessionKey.ReceivedTooLittle.selector);
        SarfSessionKey(payable(user)).executeSwap(
            address(usdt), address(aapl), 100e6, 0.3e18, address(router),
            data, 1, block.timestamp + 300, sig
        );
    }

    function test_no_standing_allowance_survives_the_swap() public {
        _buy(100e6, 0.3e18, 1);
        assertEq(usdt.allowance(user, address(router)), 0);
    }

    function test_native_okb_is_untouchable() public {
        vm.deal(user, 5 ether);
        _buy(100e6, 0.3e18, 1);
        assertEq(user.balance, 5 ether, "gas balance must not be spendable by the key");
    }

    // ------------------------------------------------------------- scoping

    function test_only_the_granted_router_may_be_called() public {
        MockRouter evil = new MockRouter();
        bytes memory data = abi.encodeCall(MockRouter.rug, (address(usdt), 100e6));
        bytes32 inner = keccak256(abi.encode(
            keccak256(
                "SarfSwap(address account,uint256 chainId,address sellToken,address buyToken,"
                "uint256 sellAmount,uint256 minBuyAmount,address target,bytes32 dataHash,"
                "uint256 nonce,uint256 deadline)"
            ),
            user, block.chainid, address(usdt), address(aapl), uint256(100e6), uint256(0),
            address(evil), keccak256(data), uint256(1), block.timestamp + 300
        ));
        (uint8 v, bytes32 r, bytes32 s) =
            vm.sign(sessionPk, keccak256(abi.encodePacked("\x19Ethereum Signed Message:\n32", inner)));
        vm.expectRevert(SarfSessionKey.TargetNotAllowed.selector);
        SarfSessionKey(payable(user)).executeSwap(
            address(usdt), address(aapl), 100e6, 0, address(evil),
            data, 1, block.timestamp + 300, abi.encodePacked(r, s, v)
        );
    }

    function test_token_outside_the_allowlist_is_refused() public {
        MockToken other = new MockToken("TSLAx", 18, false);
        bytes memory data = abi.encodeCall(
            MockRouter.swap, (address(usdt), address(other), 100e6, 1e18)
        );
        bytes memory sig = _sign(address(usdt), address(other), 100e6, 1e18, data, 1, sessionPk);
        vm.expectRevert(SarfSessionKey.TokenNotAllowed.selector);
        SarfSessionKey(payable(user)).executeSwap(
            address(usdt), address(other), 100e6, 1e18, address(router),
            data, 1, block.timestamp + 300, sig
        );
    }

    function test_a_random_key_cannot_trade() public {
        bytes memory data = abi.encodeCall(
            MockRouter.swap, (address(usdt), address(aapl), 100e6, 0.3e18)
        );
        bytes memory sig = _sign(address(usdt), address(aapl), 100e6, 0.3e18, data, 1, 0xBADBAD);
        vm.expectRevert(SarfSessionKey.BadSignature.selector);
        SarfSessionKey(payable(user)).executeSwap(
            address(usdt), address(aapl), 100e6, 0.3e18, address(router),
            data, 1, block.timestamp + 300, sig
        );
    }

    // --------------------------------------------------------------- caps

    function test_per_trade_cap() public {
        vm.expectRevert(SarfSessionKey.OverPerTradeCap.selector);
        _buy(600e6, 0, 1);
    }

    function test_daily_cap_and_its_reset() public {
        for (uint256 i = 1; i <= 4; ++i) _buy(500e6, 0, i);      // $2,000, at the cap
        vm.expectRevert(SarfSessionKey.OverDailyCap.selector);
        _buy(1e6, 0, 5);

        vm.warp(block.timestamp + 1 days);
        _buy(500e6, 0, 6);                                        // new day, allowed
        assertEq(SarfSessionKey(payable(user)).remainingToday(), DAILY - 500e6);
    }

    function test_sell_counts_its_stable_proceeds_against_the_caps() public {
        bytes memory data = abi.encodeCall(
            MockRouter.swap, (address(aapl), address(usdt), 1e18, 600e6)
        );
        bytes memory sig = _sign(address(aapl), address(usdt), 1e18, 600e6, data, 1, sessionPk);
        vm.expectRevert(SarfSessionKey.OverPerTradeCap.selector);
        SarfSessionKey(payable(user)).executeSwap(
            address(aapl), address(usdt), 1e18, 600e6, address(router),
            data, 1, block.timestamp + 300, sig
        );
    }

    // ---------------------------------------------------- replay & expiry

    function test_a_signed_swap_cannot_be_replayed() public {
        _buy(100e6, 0.3e18, 1);
        vm.expectRevert(SarfSessionKey.NonceUsed.selector);
        _buy(100e6, 0.3e18, 1);
    }

    function test_signature_malleability_is_rejected() public {
        bytes memory data = abi.encodeCall(
            MockRouter.swap, (address(usdt), address(aapl), 100e6, 0.3e18)
        );
        bytes memory sig = _sign(address(usdt), address(aapl), 100e6, 0.3e18, data, 1, sessionPk);
        bytes32 r; bytes32 s; uint8 v;
        assembly {
            r := mload(add(sig, 32))
            s := mload(add(sig, 64))
            v := byte(0, mload(add(sig, 96)))
        }
        // The equally-valid (n - s, flipped v) form must not verify.
        bytes32 s2 = bytes32(
            0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141 - uint256(s)
        );
        vm.expectRevert(SarfSessionKey.BadSignature.selector);
        SarfSessionKey(payable(user)).executeSwap(
            address(usdt), address(aapl), 100e6, 0.3e18, address(router),
            data, 1, block.timestamp + 300, abi.encodePacked(r, s2, v == 27 ? uint8(28) : uint8(27))
        );
    }

    function test_grant_expires_on_its_own() public {
        vm.warp(block.timestamp + 8 days);
        vm.expectRevert(SarfSessionKey.GrantExpired.selector);
        _buy(100e6, 0, 1);
    }

    function test_grant_cannot_outlive_the_ceiling() public {
        address[] memory toks = new address[](0);
        vm.prank(user);
        vm.expectRevert(SarfSessionKey.GrantTooLong.selector);
        SarfSessionKey(payable(user)).authorize(
            sessionKey, uint64(block.timestamp + 31 days), address(router),
            address(usdt), PER_TRADE, DAILY, toks
        );
    }

    // --------------------------------------------------------- revocation

    function test_user_can_revoke_unilaterally() public {
        vm.prank(user);
        SarfSessionKey(payable(user)).revoke();
        vm.expectRevert(SarfSessionKey.NoGrant.selector);
        _buy(100e6, 0, 1);
    }

    function test_session_key_cannot_grant_or_revoke_itself_more_power() public {
        address[] memory toks = new address[](0);
        vm.prank(sessionKey);
        vm.expectRevert(SarfSessionKey.NotSelf.selector);
        SarfSessionKey(payable(user)).authorize(
            sessionKey, uint64(block.timestamp + 1 days), address(router),
            address(usdt), type(uint128).max, type(uint128).max, toks
        );

        vm.prank(sessionKey);
        vm.expectRevert(SarfSessionKey.NotSelf.selector);
        SarfSessionKey(payable(user)).revoke();
    }

    function test_regranting_rotates_the_key_and_retires_the_old_one() public {
        address newKey = vm.addr(0xC0FFEE);
        address[] memory toks = new address[](1);
        toks[0] = address(aapl);
        vm.prank(user);
        SarfSessionKey(payable(user)).authorize(
            newKey, uint64(block.timestamp + 1 days), address(router),
            address(usdt), PER_TRADE, DAILY, toks
        );
        vm.expectRevert(SarfSessionKey.BadSignature.selector);
        _buy(100e6, 0, 1);  // still signing with the retired key
    }
}

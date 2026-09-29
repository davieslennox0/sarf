// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {SarfDelegate} from "../src/SarfDelegate.sol";

/// @dev USDT-style: `approve` returns nothing. Both zap legs and the park
///      asset behave this way on X Layer, so a well-behaved mock would be
///      testing the wrong token.
contract MockToken {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    bool public immutable silent;

    constructor(bool s) { silent = s; }

    function mint(address to, uint256 v) external { balanceOf[to] += v; }

    function approve(address sp, uint256 v) external returns (bool) {
        allowance[msg.sender][sp] = v;
        if (silent) assembly { return(0, 0) }
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

/// @dev Stands in for a pool: takes the LP/pool token and returns the other
///      side to whoever it is told to pay. `to` is a parameter precisely
///      because that is where a real router's drain lives.
contract MockPool {
    function exit(address tokenIn, uint256 amtIn, address tokenOut, uint256 amtOut, address to) external {
        MockToken(tokenIn).transferFrom(msg.sender, address(this), amtIn);
        MockToken(tokenOut).transfer(to, amtOut);
    }
    function takeOnly(address tokenIn, uint256 amtIn) external {
        MockToken(tokenIn).transferFrom(msg.sender, address(this), amtIn);
    }
}

contract SarfDelegateTest is Test {
    SarfDelegate impl;

    uint256 userPk = 0xA11CE;
    address user;
    address keeper = address(0xC0FFEE);
    address attacker = address(0xBAD);

    MockToken lp;    // what an exit burns
    MockToken usdt;  // what it parks into
    MockPool pool;

    SarfDelegate acct; // the user's own account, running this code

    function setUp() public {
        impl = new SarfDelegate();
        user = vm.addr(userPk);
        lp = new MockToken(false);
        usdt = new MockToken(true); // USDT-style silent approve
        pool = new MockPool();

        lp.mint(user, 1_000e18);
        usdt.mint(address(pool), 1_000_000e6);

        // The real thing: an EIP-7702 delegation of the user's own EOA.
        vm.signAndAttachDelegation(address(impl), userPk);
        acct = SarfDelegate(payable(user));

        _scope(keeper, uint64(block.timestamp + 7 days), 100e18, 300e18);
    }

    function _spec(address k, uint64 expiry, uint128 perAction, uint128 daily)
        internal view returns (SarfDelegate.ScopeSpec memory spec)
    {
        address[] memory targets = new address[](1);
        targets[0] = address(pool);
        address[] memory tokens = new address[](1);
        tokens[0] = address(lp);
        uint128[] memory pa = new uint128[](1);
        pa[0] = perAction;
        uint128[] memory dy = new uint128[](1);
        dy[0] = daily;
        spec = SarfDelegate.ScopeSpec({
            keeper: k, expiry: expiry, targets: targets, tokens: tokens, perAction: pa, daily: dy
        });
    }

    function _empty() internal pure returns (SarfDelegate.ScopeSpec memory spec) {
        spec = SarfDelegate.ScopeSpec({
            keeper: address(0), expiry: 0, targets: new address[](0), tokens: new address[](0),
            perAction: new uint128[](0), daily: new uint128[](0)
        });
    }

    function _scope(address k, uint64 expiry, uint128 perAction, uint128 daily) internal {
        vm.prank(user); // under 7702 the user's own tx has msg.sender == the account
        acct.setScope(_spec(k, expiry, perAction, daily));
    }

    function _legs(uint256 amt) internal view returns (SarfDelegate.Leg[] memory l) {
        l = new SarfDelegate.Leg[](1);
        l[0] = SarfDelegate.Leg({token: address(lp), amount: amt, spender: address(0)});
    }

    function _wants(uint256 minOut) internal view returns (SarfDelegate.Want[] memory w) {
        w = new SarfDelegate.Want[](1);
        w[0] = SarfDelegate.Want({token: address(usdt), minOut: minOut});
    }

    function _exitCall(uint256 amtIn, uint256 amtOut, address to) internal view returns (bytes memory) {
        return abi.encodeCall(MockPool.exit, (address(lp), amtIn, address(usdt), amtOut, to));
    }

    // ------------------------------------------------------------ happy path

    function test_keeper_can_exit_a_position_for_the_user() public {
        vm.prank(keeper);
        acct.execute(address(pool), _exitCall(50e18, 60e6, user), _legs(50e18), _wants(60e6));

        assertEq(lp.balanceOf(user), 950e18);
        assertEq(usdt.balanceOf(user), 60e6, "proceeds land in the user's own account");
        assertEq(lp.allowance(user, address(pool)), 0, "no standing allowance survives");
        assertEq(acct.remainingToday(address(lp)), 250e18);
    }

    function test_the_user_never_signs_the_action() public {
        // No signature anywhere in execute(): the keeper's own tx is the call.
        vm.prank(keeper);
        acct.execute(address(pool), _exitCall(10e18, 12e6, user), _legs(10e18), _wants(12e6));
        assertEq(usdt.balanceOf(user), 12e6);
    }

    // --------------------------------------------------------- the hard rule

    function test_drain_to_an_external_address_reverts() public {
        // Same call, proceeds routed to the attacker. This is THE test.
        vm.prank(keeper);
        vm.expectRevert(SarfDelegate.ReceivedTooLittle.selector);
        acct.execute(address(pool), _exitCall(50e18, 60e6, attacker), _legs(50e18), _wants(60e6));

        assertEq(usdt.balanceOf(attacker), 0);
        assertEq(lp.balanceOf(user), 1_000e18, "the whole action reverted, nothing left");
    }

    function test_keeper_cannot_hide_a_drain_by_asking_for_nothing() public {
        // minOut = 0 is the obvious dodge: declare no output and send it away.
        // The spend side still has to be paid for, so the loss is capped by
        // the per-action cap, and the LP leaving at all is what costs the
        // keeper its daily allowance.
        vm.prank(keeper);
        acct.execute(address(pool), _exitCall(50e18, 60e6, attacker), _legs(50e18), _wants(0));

        assertEq(usdt.balanceOf(attacker), 60e6, "declaring no output cannot be prevented here");
        assertEq(acct.remainingToday(address(lp)), 250e18, "but it is paid for out of the cap");
    }

    function test_output_must_arrive_in_full() public {
        vm.prank(keeper);
        vm.expectRevert(SarfDelegate.ReceivedTooLittle.selector);
        acct.execute(address(pool), _exitCall(50e18, 59e6, user), _legs(50e18), _wants(60e6));
    }

    function test_spending_more_than_declared_reverts() public {
        // The call takes 80 but only 50 was declared and approved.
        vm.prank(keeper);
        vm.expectRevert(); // fails inside the pool for want of allowance
        acct.execute(address(pool), _exitCall(80e18, 60e6, user), _legs(50e18), _wants(60e6));
        assertEq(lp.balanceOf(user), 1_000e18);
    }

    // ------------------------------------------------------------- the gates

    function test_non_whitelisted_target_reverts() public {
        MockPool rogue = new MockPool();
        vm.prank(keeper);
        vm.expectRevert(SarfDelegate.TargetNotAllowed.selector);
        acct.execute(address(rogue), _exitCall(10e18, 1e6, user), _legs(10e18), _wants(0));
    }

    function test_target_cannot_be_the_account_itself() public {
        // Otherwise the inner call arrives with msg.sender == address(this)
        // and satisfies onlyAccount — the keeper would own the account.
        bytes memory reScope = abi.encodeCall(
            SarfDelegate.registerPasskey, (uint256(1), uint256(2))
        );
        vm.prank(keeper);
        vm.expectRevert(SarfDelegate.TargetIsSelf.selector);
        acct.execute(user, reScope, new SarfDelegate.Leg[](0), new SarfDelegate.Want[](0));
    }

    function test_only_the_keeper_may_execute() public {
        vm.prank(attacker);
        vm.expectRevert(SarfDelegate.NotKeeper.selector);
        acct.execute(address(pool), _exitCall(1e18, 1e6, user), _legs(1e18), _wants(0));

        vm.prank(user);
        vm.expectRevert(SarfDelegate.NotKeeper.selector);
        acct.execute(address(pool), _exitCall(1e18, 1e6, user), _legs(1e18), _wants(0));
    }

    function test_only_the_account_may_scope_or_revoke() public {
        SarfDelegate.ScopeSpec memory spec = _spec(attacker, uint64(block.timestamp + 1 days), 1, 1);
        vm.prank(keeper);
        vm.expectRevert(SarfDelegate.NotSelf.selector);
        acct.setScope(spec);

        vm.prank(keeper);
        vm.expectRevert(SarfDelegate.NotSelf.selector);
        acct.revoke();
    }

    function test_expired_scope_reverts() public {
        vm.warp(block.timestamp + 8 days);
        vm.prank(keeper);
        vm.expectRevert(SarfDelegate.ScopeExpired.selector);
        acct.execute(address(pool), _exitCall(10e18, 1e6, user), _legs(10e18), _wants(0));
    }

    function test_scope_cannot_outlive_the_ceiling() public {
        SarfDelegate.ScopeSpec memory spec = _spec(keeper, uint64(block.timestamp + 31 days), 1, 1);
        vm.prank(user);
        vm.expectRevert(SarfDelegate.ScopeTooLong.selector);
        acct.setScope(spec);
    }

    function test_revoked_scope_reverts_and_drops_targets_and_caps() public {
        vm.prank(user);
        acct.revoke();

        vm.prank(keeper);
        vm.expectRevert(SarfDelegate.NoScope.selector);
        acct.execute(address(pool), _exitCall(10e18, 1e6, user), _legs(10e18), _wants(0));

        assertFalse(acct.allowedTarget(address(pool)), "whitelist gone in one call");
        (uint128 pa,) = acct.capOf(address(lp));
        assertEq(pa, 0, "caps gone with it");
        assertEq(acct.remainingToday(address(lp)), 0);
    }

    function test_redelegating_to_zero_disables_everything() public {
        // The other revocation path: point the 7702 delegation at 0x0. The
        // account stops being this contract at all.
        vm.signAndAttachDelegation(address(0), userPk);
        assertEq(user.code.length, 0, "no delegate code left");

        // The account is a plain EOA again: a call to it executes nothing at
        // all (calls to code-less addresses succeed and do nothing), so the
        // keeper's call cannot move a token either way.
        uint256 lpBefore = lp.balanceOf(user);
        vm.prank(keeper);
        (bool ok,) = user.call(
            abi.encodeCall(SarfDelegate.execute,
                (address(pool), _exitCall(10e18, 1e6, user), _legs(10e18), _wants(0)))
        );
        assertTrue(ok, "a call to a code-less EOA does nothing and reverts nothing");
        assertEq(lp.balanceOf(user), lpBefore, "and moves nothing");
    }

    function test_per_action_cap_breach_reverts() public {
        vm.prank(keeper);
        vm.expectRevert(SarfDelegate.OverPerActionCap.selector);
        acct.execute(address(pool), _exitCall(101e18, 1e6, user), _legs(101e18), _wants(0));
    }

    function test_daily_cap_breach_reverts_and_resets_next_day() public {
        for (uint256 i; i < 3; ++i) {
            vm.prank(keeper);
            acct.execute(address(pool), _exitCall(100e18, 1e6, user), _legs(100e18), _wants(0));
        }
        assertEq(acct.remainingToday(address(lp)), 0);

        vm.prank(keeper);
        vm.expectRevert(SarfDelegate.OverDailyCap.selector);
        acct.execute(address(pool), _exitCall(1e18, 0, user), _legs(1e18), _wants(0));

        vm.warp(block.timestamp + 1 days);
        assertEq(acct.remainingToday(address(lp)), 300e18, "new day, fresh allowance");
    }

    function test_token_without_a_cap_cannot_be_spent() public {
        MockToken other = new MockToken(false);
        other.mint(user, 100e18);
        SarfDelegate.Leg[] memory l = new SarfDelegate.Leg[](1);
        l[0] = SarfDelegate.Leg({token: address(other), amount: 1e18, spender: address(0)});
        vm.prank(keeper);
        vm.expectRevert(SarfDelegate.TokenNotAllowed.selector);
        acct.execute(address(pool), _exitCall(1e18, 0, user), l, _wants(0));
    }

    function test_native_okb_is_never_sent() public {
        vm.deal(user, 5 ether);
        vm.prank(keeper);
        acct.execute(address(pool), _exitCall(10e18, 12e6, user), _legs(10e18), _wants(12e6));
        assertEq(user.balance, 5 ether, "gas balance untouched");
        assertEq(address(pool).balance, 0);
    }

    // ------------------------------------------------- keeper-key compromise

    /// The question that decides whether this is shippable: a stolen keeper
    /// key, used as destructively as the contract permits, all day.
    function test_worst_case_of_a_stolen_keeper_key_is_bounded_by_caps() public {
        uint256 lpBefore = lp.balanceOf(user);

        // The attacker owns the keeper key. It cannot call a target the user
        // did not whitelist, cannot touch a token the user did not cap, and
        // cannot reach the owner-only functions.
        vm.startPrank(keeper);
        for (uint256 i; i < 50; ++i) {
            // Burn value as fast as the contract allows: send proceeds away.
            try acct.execute(address(pool), _exitCall(100e18, 0, attacker), _legs(100e18), _wants(0)) {
            } catch { break; }
        }
        vm.stopPrank();

        uint256 lost = lpBefore - lp.balanceOf(user);
        assertEq(lost, 300e18, "cannot exceed one day's cap, whatever it does");
        assertEq(acct.remainingToday(address(lp)), 0);

        // And the user ends it unilaterally, without Sarf.
        vm.prank(user);
        acct.revoke();
        vm.prank(keeper);
        vm.expectRevert(SarfDelegate.NoScope.selector);
        acct.execute(address(pool), _exitCall(1e18, 0, attacker), _legs(1e18), _wants(0));
    }

    function test_compromised_keeper_cannot_extend_its_own_scope() public {
        SarfDelegate.ScopeSpec memory spec =
            _spec(keeper, uint64(block.timestamp + 30 days), type(uint128).max, type(uint128).max);
        vm.prank(keeper);
        vm.expectRevert(SarfDelegate.NotSelf.selector);
        acct.setScope(spec);
    }

    function test_allowance_can_go_to_a_separate_spender_but_only_a_named_one() public {
        // The aggregator shape: the call goes to the router, the allowance has
        // to sit on its collector.
        MockPool collector = new MockPool();
        SarfDelegate.Leg[] memory l = new SarfDelegate.Leg[](1);
        l[0] = SarfDelegate.Leg({token: address(lp), amount: 10e18, spender: address(collector)});

        vm.prank(keeper);
        vm.expectRevert(SarfDelegate.TargetNotAllowed.selector);
        acct.execute(address(pool), _exitCall(0, 0, user), l, _wants(0));

        // Whitelist it and the same action is allowed.
        address[] memory targets = new address[](2);
        targets[0] = address(pool);
        targets[1] = address(collector);
        address[] memory tokens = new address[](1);
        tokens[0] = address(lp);
        uint128[] memory pa = new uint128[](1);
        pa[0] = 100e18;
        uint128[] memory dy = new uint128[](1);
        dy[0] = 300e18;
        vm.prank(user);
        acct.setScope(SarfDelegate.ScopeSpec({
            keeper: keeper, expiry: uint64(block.timestamp + 7 days),
            targets: targets, tokens: tokens, perAction: pa, daily: dy
        }));

        vm.prank(keeper);
        acct.execute(address(pool), _exitCall(0, 0, user), l, _wants(0));
        assertEq(lp.allowance(user, address(collector)), 0, "and is zeroed afterwards");
    }

    // Passkey tests live in SarfDelegateFork.t.sol: the RIP-7212 precompile
    // is a property of the chain, and Foundry's local EVM does not have it.
    // Testing it against a mock would prove nothing about X Layer.

}

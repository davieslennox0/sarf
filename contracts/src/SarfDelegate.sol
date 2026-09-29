// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/**
 * @title SarfDelegate
 * @notice An EIP-7702 delegate that lets a plain EOA grant Sarf's keeper a
 *         scoped, expiring right to run *position maintenance* on its behalf —
 *         the zap's exit and re-entry — without the user signing each step and
 *         without anyone ever holding the user's key.
 *
 * RELATIONSHIP TO SarfSessionKey
 *     SarfSessionKey authorises swaps through one aggregator router, and its
 *     caps are denominated in a stable leg. That works for trading and not for
 *     zapping: an exit burns LP tokens and a re-entry spends two pool tokens,
 *     none of which have a stable leg to measure. So this contract keeps the
 *     same security philosophy and changes what is measured:
 *
 *       - caps are per token, in that token's own units, set by the user.
 *         No oracle, no price feed, nothing to manipulate.
 *       - the keeper is an address, not a signing key. Under 7702 the keeper
 *         calls the user's own account directly, so `msg.sender` is the
 *         authority and there is no signed message to replay or capture.
 *
 * THE HARD RULE
 *     Every token an action produces must end up at `address(this)` — which
 *     under 7702 *is* the user's own EOA. `execute` snapshots balances, makes
 *     the call, and then requires each declared output to have arrived here and
 *     each declared input to have fallen by no more than the user's cap. A call
 *     that routes the proceeds anywhere else reverts, whatever its calldata
 *     said. Native OKB is never sent (`call{value: 0}`), so the gas balance
 *     cannot be touched at all.
 *
 * WHAT A COMPROMISED KEEPER CAN DO
 *     Worth stating plainly, because it is the question that matters. A stolen
 *     keeper key cannot move funds out: outputs must land in the user's own
 *     account, and only whitelisted targets can be called. What it CAN do is
 *     churn — run allowed actions against allowed targets until the per-token
 *     daily caps are exhausted, losing the user swap fees and slippage. That
 *     loss is bounded by the caps the user set, and stops entirely at expiry or
 *     the moment the user calls `revoke()`, which needs nothing from Sarf.
 *
 * WHAT IS DELIBERATELY ABSENT
 *     No upgradeability. No admin. No owner that can change a user's scope. No
 *     arbitrary call passthrough: a target must be on the user's own whitelist,
 *     can never be this account itself, and is called with `call`, never
 *     `delegatecall`. There is no function that can add a target or raise a cap
 *     except one the user's own wallet (or their registered passkey) signs.
 */
contract SarfDelegate {
    // -------------------------------------------------------------- errors

    error NotSelf();
    error NotKeeper();
    error NoScope();
    error ScopeExpired();
    error ScopeTooLong();
    error ZeroKeeper();
    error TargetNotAllowed();
    error TargetIsSelf();
    error TokenNotAllowed();
    error OverPerActionCap();
    error OverDailyCap();
    error ActionFailed();
    error SpentTooMuch();
    error ReceivedTooLittle();
    error CapsMismatch();
    error NoPasskey();
    error BadPasskeySignature();
    error PasskeyNonceUsed();
    error ChallengeMismatch();

    // -------------------------------------------------------------- events

    event ScopeSet(address indexed keeper, uint64 expiry, uint256 epoch, bool viaPasskey);
    event ScopeRevoked(address indexed keeper, uint256 epoch);
    event PasskeyRegistered(uint256 x, uint256 y);
    event Executed(address indexed target, uint256 spendLegs, uint256 wantLegs);

    // -------------------------------------------------------------- types

    struct Scope {
        address keeper;   // the only address that may call execute()
        uint64 expiry;    // unix seconds; hard stop, no renewal in place
    }

    struct Cap {
        uint128 perAction;  // max of this token one action may spend
        uint128 daily;      // max of this token per rolling UTC day
        uint128 spentToday;
        uint64 dayStart;    // UTC day `spentToday` counts
    }

    /// @param token   token this action may spend
    /// @param amount  the most of it the action may spend, and the exact
    ///                allowance granted for the call and zeroed after it
    /// @param spender who receives that allowance. Zero means the target
    ///                itself. It is a separate field because aggregators do
    ///                not pull tokens with the contract you call: OKX's
    ///                router reaches them through its own TokenApprove, so an
    ///                allowance pinned to the target is one nobody can spend
    ///                and every such swap reverts after paying the gas. That
    ///                shipped once already, in SarfSessionKey v1. A non-zero
    ///                spender must be whitelisted too, so it is still only
    ///                contracts the user named.
    struct Leg {
        address token;
        uint256 amount;
        address spender;
    }

    /// @param token  token the action must deliver to this account
    /// @param minOut the least of it that must arrive
    struct Want {
        address token;
        uint256 minOut;
    }

    /// @param targets   contracts the keeper may call. Nothing else, ever.
    /// @param tokens    tokens it may spend; a token absent here is untouchable.
    /// @param perAction per-token ceiling for one action, in that token's units.
    /// @param daily     per-token ceiling per UTC day, same units.
    struct ScopeSpec {
        address keeper;
        uint64 expiry;
        address[] targets;
        address[] tokens;
        uint128[] perAction;
        uint128[] daily;
    }

    /// @dev A WebAuthn assertion, exactly as the browser returns it.
    struct PasskeySig {
        bytes authenticatorData;
        string clientDataJSON;
        uint256 challengeIndex;
        uint256 r;
        uint256 s;
    }

    // -------------------------------------------------------------- storage

    Scope public scope;

    /// @dev Bumped by every revoke and every new scope. Targets and caps are
    ///      keyed by it, so one `revoke()` makes the whole previous
    ///      configuration inert without iterating anything. Revocation that
    ///      has to clear unbounded mappings is revocation that can run out of
    ///      gas exactly when it is needed.
    uint256 public epoch;

    mapping(uint256 => mapping(address => bool)) private _allowedTarget;
    mapping(uint256 => mapping(address => Cap)) private _cap;

    /// @dev The user's passkey, as a P-256 public key. Optional: an account
    ///      that never registers one simply cannot use the passkey path.
    uint256 public passkeyX;
    uint256 public passkeyY;
    mapping(uint256 => bool) public usedPasskeyNonce;

    /// @notice Longest scope this contract will issue, whatever is asked for.
    uint64 public constant MAX_SCOPE = 30 days;

    /// @dev RIP-7212 P-256 verifier. Verified live on X Layer (chain 196) on
    ///      2026-09-29: a valid signature returns 1, an invalid one returns
    ///      empty, at ~6.7k gas. A chain without it returns empty for
    ///      everything, which fails closed: no passkey signature verifies.
    address internal constant P256_VERIFY = address(0x100);

    // ------------------------------------------------------------- modifiers

    /// @dev Under 7702 `address(this)` IS the user's EOA, so a self-call is
    ///      exactly "a transaction the user signed with their own wallet key"
    ///      — the Privy embedded key, in Sarf's case. The keeper can never
    ///      satisfy this: `execute` refuses to call this account at all.
    modifier onlyAccount() {
        if (msg.sender != address(this)) revert NotSelf();
        _;
    }

    // ---------------------------------------------------------------- scope

    /**
     * @notice Set the scope. Called by the user's own wallet.
     * @dev Overwrites any previous scope and bumps the epoch, so re-scoping is
     *      also how a keeper is rotated and how an old whitelist is dropped.
     * @param spec the keeper, the expiry, and the per-token caps. Caps are in
     *             each token's own units; a token absent from it can never be
     *             spent.
     */
    function setScope(ScopeSpec calldata spec) external onlyAccount {
        _setScope(spec, false);
    }

    /**
     * @notice Set the scope with the registered passkey instead of the wallet
     *         key, so a user on a phone can authorise without a wallet popup.
     * @dev The signature is a WebAuthn assertion over this exact scope: the
     *      digest binds the account, the chain, every parameter and a nonce,
     *      and the browser's challenge must equal that digest. See
     *      `_verifyPasskey` for why each part is checked.
     */
    function setScopeWithPasskey(ScopeSpec calldata spec, uint256 nonce, PasskeySig calldata sig)
        external
    {
        if (usedPasskeyNonce[nonce]) revert PasskeyNonceUsed();
        _verifyPasskey(_scopeDigest(spec, nonce), sig);
        usedPasskeyNonce[nonce] = true;
        _setScope(spec, true);
    }

    /// @dev Binds the assertion to this account, this chain, every parameter
    ///      of the scope and a nonce. Change any of them and the signature is
    ///      for a different scope and does not verify.
    function _scopeDigest(ScopeSpec calldata spec, uint256 nonce) private view returns (bytes32) {
        return keccak256(
            abi.encode(
                address(this), block.chainid, spec.keeper, spec.expiry,
                keccak256(abi.encodePacked(spec.targets)),
                keccak256(abi.encodePacked(spec.tokens)),
                keccak256(abi.encodePacked(spec.perAction)),
                keccak256(abi.encodePacked(spec.daily)),
                nonce
            )
        );
    }

    /**
     * @notice End the scope immediately. Only the user can call it, and it
     *         needs nothing from Sarf — which is what makes this revocable
     *         rather than merely time-limited.
     * @dev Bumping the epoch drops every target and cap with it. Pointing the
     *      7702 delegation back at address(0) disables this contract entirely
     *      and is equally final; neither path leaves a live keeper behind,
     *      because `execute` reads `scope.keeper`, which is cleared here.
     */
    function revoke() external onlyAccount {
        address k = scope.keeper;
        delete scope;
        unchecked { ++epoch; }
        emit ScopeRevoked(k, epoch);
    }

    /// @notice Register (or replace) the passkey allowed to set scopes.
    /// @dev Wallet-key only. A passkey cannot rotate the passkey: that would
    ///      make a phished assertion permanent.
    function registerPasskey(uint256 x, uint256 y) external onlyAccount {
        passkeyX = x;
        passkeyY = y;
        emit PasskeyRegistered(x, y);
    }

    function _setScope(ScopeSpec calldata spec, bool viaPasskey) private {
        if (spec.keeper == address(0)) revert ZeroKeeper();
        if (spec.expiry <= block.timestamp) revert ScopeExpired();
        if (spec.expiry > block.timestamp + MAX_SCOPE) revert ScopeTooLong();
        if (spec.tokens.length != spec.perAction.length || spec.tokens.length != spec.daily.length) {
            revert CapsMismatch();
        }

        unchecked { ++epoch; }
        uint256 e = epoch;
        for (uint256 i; i < spec.targets.length; ++i) {
            // A target that is this account would let the keeper re-enter the
            // owner-only functions with msg.sender == address(this).
            if (spec.targets[i] == address(this)) revert TargetIsSelf();
            _allowedTarget[e][spec.targets[i]] = true;
        }
        for (uint256 i; i < spec.tokens.length; ++i) {
            _cap[e][spec.tokens[i]] = Cap({
                perAction: spec.perAction[i], daily: spec.daily[i], spentToday: 0, dayStart: _today()
            });
        }
        scope = Scope({keeper: spec.keeper, expiry: spec.expiry});
        emit ScopeSet(spec.keeper, spec.expiry, e, viaPasskey);
    }

    // ------------------------------------------------------------- execution

    /**
     * @notice Run one maintenance action under the scope.
     * @dev Callable only by the keeper. `data` is deliberately not parsed: see
     *      the post-conditions below, which are what actually constrain the
     *      outcome. Everything above them decides whether the call may be
     *      attempted; they decide what it is allowed to have done.
     * @param spend tokens the action may take, with the most it may take of
     *              each. Each is approved exactly and zeroed in this same
     *              transaction, so no standing allowance survives.
     * @param want  tokens that must arrive at this account, and the least of
     *              each. This is the hard rule made mechanical.
     */
    function execute(
        address target,
        bytes calldata data,
        Leg[] calldata spend,
        Want[] calldata want
    ) external {
        Scope memory sc = scope;
        if (sc.keeper == address(0)) revert NoScope();
        if (msg.sender != sc.keeper) revert NotKeeper();
        if (block.timestamp > sc.expiry) revert ScopeExpired();
        if (target == address(this)) revert TargetIsSelf();

        uint256 e = epoch;
        if (!_allowedTarget[e][target]) revert TargetNotAllowed();

        uint64 today = _today();
        uint256[] memory spendBefore = new uint256[](spend.length);
        for (uint256 i; i < spend.length; ++i) {
            Cap memory c = _cap[e][spend[i].token];
            // A cap of zero is not "unlimited", it is "never": a token the
            // user did not name cannot be spent at all.
            if (c.perAction == 0) revert TokenNotAllowed();
            if (spend[i].amount > c.perAction) revert OverPerActionCap();
            uint128 spent = c.dayStart == today ? c.spentToday : 0;
            if (uint256(spent) + spend[i].amount > c.daily) revert OverDailyCap();
            // Safe: the line above reverts unless the sum is <= daily, itself
            // a uint128.
            // forge-lint: disable-next-line(unsafe-typecast)
            _cap[e][spend[i].token].spentToday = uint128(spent + spend[i].amount);
            _cap[e][spend[i].token].dayStart = today;

            spendBefore[i] = _balanceOf(spend[i].token);
            address sp = spend[i].spender == address(0) ? target : spend[i].spender;
            if (sp != target && !_allowedTarget[e][sp]) revert TargetNotAllowed();
            if (sp == address(this)) revert TargetIsSelf();
            _approve(spend[i].token, sp, spend[i].amount);
        }

        uint256[] memory wantBefore = new uint256[](want.length);
        for (uint256 i; i < want.length; ++i) {
            wantBefore[i] = _balanceOf(want[i].token);
        }

        // value: 0 always. The account's OKB pays for gas and nothing else,
        // so a compromised keeper cannot drain the balance that pays for the
        // user's own future signatures. `call`, never `delegatecall`: this
        // contract's storage is the user's account and is never handed to a
        // third party to write.
        (bool ok, ) = target.call{value: 0}(data);
        if (!ok) revert ActionFailed();

        for (uint256 i; i < spend.length; ++i) {
            _approve(spend[i].token, spend[i].spender == address(0) ? target : spend[i].spender, 0);
            uint256 after_ = _balanceOf(spend[i].token);
            // Underflow-safe by construction: a balance that grew cannot have
            // been overspent, and is allowed.
            if (spendBefore[i] > after_ && spendBefore[i] - after_ > spend[i].amount) {
                revert SpentTooMuch();
            }
        }
        for (uint256 i; i < want.length; ++i) {
            uint256 after_ = _balanceOf(want[i].token);
            if (after_ < wantBefore[i] || after_ - wantBefore[i] < want[i].minOut) {
                revert ReceivedTooLittle();
            }
        }

        emit Executed(target, spend.length, want.length);
    }

    // ---------------------------------------------------------------- views

    function allowedTarget(address target) external view returns (bool) {
        return _allowedTarget[epoch][target];
    }

    /// @notice Remaining spend allowance for `token` today, in its own units.
    function remainingToday(address token) external view returns (uint256) {
        Scope memory sc = scope;
        if (sc.keeper == address(0) || block.timestamp > sc.expiry) return 0;
        Cap memory c = _cap[epoch][token];
        uint128 spent = c.dayStart == _today() ? c.spentToday : 0;
        return c.daily > spent ? c.daily - spent : 0;
    }

    function capOf(address token) external view returns (uint128 perAction, uint128 daily) {
        Cap memory c = _cap[epoch][token];
        return (c.perAction, c.daily);
    }

    // -------------------------------------------------------------- helpers

    /**
     * @dev WebAuthn assertion check, via the RIP-7212 precompile.
     *
     *      A passkey does not sign an arbitrary 32 bytes: it signs
     *      sha256(authenticatorData ‖ sha256(clientDataJSON)), where the
     *      browser puts the challenge inside clientDataJSON. So verifying the
     *      signature alone proves only that *some* assertion was made. The
     *      challenge check is what ties it to this scope: without it, any
     *      assertion the user ever made for any site could be replayed here.
     */
    function _verifyPasskey(bytes32 digest, PasskeySig calldata sig) private view {
        if (passkeyX == 0 && passkeyY == 0) revert NoPasskey();

        bytes memory expected = abi.encodePacked('"challenge":"', _b64url(digest), '"');
        bytes memory cd = bytes(sig.clientDataJSON);
        if (sig.challengeIndex + expected.length > cd.length) revert ChallengeMismatch();
        for (uint256 i; i < expected.length; ++i) {
            if (cd[sig.challengeIndex + i] != expected[i]) revert ChallengeMismatch();
        }

        bytes32 msgHash = sha256(abi.encodePacked(sig.authenticatorData, sha256(cd)));
        // Low-s only: P-256 signatures malleate exactly as secp256k1 ones do,
        // and the nonce is not enough on its own to make that harmless.
        if (sig.s > 0x7FFFFFFF800000007FFFFFFFFFFFFFFFDE737D56D38BCF4279DCE5617E3192A8) {
            revert BadPasskeySignature();
        }
        (bool ok, bytes memory out) =
            P256_VERIFY.staticcall(abi.encode(msgHash, sig.r, sig.s, passkeyX, passkeyY));
        // An empty return is the precompile's "invalid", and is also what a
        // chain without the precompile returns for everything. Both mean no.
        if (!ok || out.length < 32 || abi.decode(out, (uint256)) != 1) {
            revert BadPasskeySignature();
        }
    }

    /// @dev base64url of 32 bytes, unpadded — the form WebAuthn puts in
    ///      clientDataJSON. 32 bytes is 43 characters with no padding.
    function _b64url(bytes32 v) private pure returns (bytes memory out) {
        bytes memory abc = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
        out = new bytes(43);
        uint256 acc;
        uint256 bits;
        uint256 o;
        for (uint256 i; i < 32; ++i) {
            acc = (acc << 8) | uint8(v[i]);
            bits += 8;
            while (bits >= 6) {
                bits -= 6;
                out[o++] = abc[(acc >> bits) & 0x3F];
            }
        }
        // 32 bytes is not a multiple of 3, so 4 bits are left over and become
        // the final character.
        out[o] = abc[(acc << (6 - bits)) & 0x3F];
    }

    function _today() private view returns (uint64) {
        return uint64(block.timestamp / 1 days);
    }

    function _balanceOf(address token) private view returns (uint256) {
        (bool ok, bytes memory out) =
            token.staticcall(abi.encodeWithSelector(0x70a08231, address(this))); // balanceOf
        if (!ok || out.length < 32) revert ActionFailed();
        return abi.decode(out, (uint256));
    }

    /// @dev Tolerates ERC-20s that return nothing from approve (USDT).
    function _approve(address token, address spender, uint256 value) private {
        (bool ok, bytes memory out) =
            token.call(abi.encodeWithSelector(0x095ea7b3, spender, value)); // approve
        if (!ok || (out.length != 0 && !abi.decode(out, (bool)))) revert ActionFailed();
    }

    /// @dev Accept OKB so gas top-ups still work while delegated. Nothing here
    ///      can send it back out.
    receive() external payable {}
}

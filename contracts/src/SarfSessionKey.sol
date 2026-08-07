// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/**
 * @title SarfSessionKey
 * @notice An EIP-7702 delegate that lets a plain EOA authorise a scoped,
 *         expiring trading key — so an assistant can execute swaps inside a
 *         chat without the user re-signing every trade, and without anyone
 *         ever holding the user's wallet key.
 *
 * CUSTODY
 *     The user's private key never leaves their wallet. They sign the 7702
 *     authorisation and the grant themselves, and `revoke()` is theirs alone
 *     — it needs no cooperation from Sarf. The session key Sarf holds has no
 *     authority of its own; every power it has is written into the grant
 *     below and expires on schedule.
 *
 * HOW IT RUNS
 *     Under EIP-7702 the user signs a type-4 authorisation pointing their EOA
 *     at this implementation. Calls to the user's address then execute this
 *     code with `address(this) == the user's own address`, and storage reads
 *     and writes land in the user's own account. There is no proxy, no
 *     migration, and no moving of funds.
 *
 * THE SECURITY MODEL IS POST-CONDITIONS, NOT CALLDATA PARSING
 *     A session key that could be talked into calling `approve(attacker, ...)`
 *     or `transfer(attacker, ...)` would be worth exactly as much as the whole
 *     wallet. Aggregator calldata is opaque, multi-hop and version-dependent,
 *     so validating it field by field is a losing game — one router upgrade and
 *     the parser is wrong in a way that fails open.
 *
 *     So `executeSwap` does not try to understand the calldata. It measures.
 *     It snapshots both token balances, performs the call, and then *requires*
 *     that no more than `sellAmount` left and at least `minBuyAmount` arrived.
 *     Whatever the calldata says, those two assertions are what the transaction
 *     is allowed to have done. The approval it grants is exact and is zeroed in
 *     the same transaction, so nothing survives the call. Native OKB is never
 *     sent (`call{value: 0}`), so the gas balance is untouchable.
 *
 *     Consequence worth stating plainly: a stolen session key cannot move funds
 *     out. The worst it can do is trade the user's allowed tokens, at honest
 *     prices enforced by minBuyAmount, up to the caps, until the grant expires.
 */
contract SarfSessionKey {
    // -------------------------------------------------------------- errors

    error NotSelf();
    error NoGrant();
    error GrantExpired();
    error KeyMismatch();
    error BadSignature();
    error NonceUsed();
    error Deadline();
    error TokenNotAllowed();
    error TargetNotAllowed();
    error OverPerTradeCap();
    error OverDailyCap();
    error SwapFailed();
    error SoldTooMuch();
    error ReceivedTooLittle();
    error ZeroKey();
    error GrantTooLong();

    // -------------------------------------------------------------- events

    event Granted(address indexed sessionKey, uint64 expiry, uint128 perTradeCap, uint128 dailyCap);
    event Revoked(address indexed sessionKey);
    event Swapped(
        address indexed sellToken,
        address indexed buyToken,
        uint256 sellAmount,
        uint256 bought,
        uint256 countedAgainstCaps
    );

    // -------------------------------------------------------------- storage

    /// @dev A grant is one struct so it can be wiped in a single `delete`.
    ///      Revocation that has to clear several slots is revocation that can
    ///      half-fail.
    struct Grant {
        address sessionKey;    // the only key that may call executeSwap
        uint64 expiry;         // unix seconds; hard stop, no renewal in place
        address router;        // the single contract a swap may be routed to
        address stable;        // the unit every cap below is denominated in
        uint128 perTradeCap;   // max stable value of one trade
        uint128 dailyCap;      // max stable value per rolling UTC day
        uint128 spentToday;
        uint64 dayStart;       // UTC midnight of the day `spentToday` counts
    }

    Grant public grant;

    /// @dev token => may be traded under this grant. Deliberately not cleared
    ///      on revoke: the allowlist is inert without a live grant, and
    ///      clearing an unbounded mapping is a gas-bomb that could make
    ///      revocation fail exactly when it is needed most.
    mapping(address => bool) public allowedToken;

    /// @dev Signed nonces already spent. Replay of a signed swap would let a
    ///      captured message be re-run against a fresh daily allowance.
    mapping(uint256 => bool) public usedNonce;

    /// @notice Longest grant this contract will issue, regardless of what is
    ///         asked for. A key that outlives the user's memory of granting it
    ///         is the failure mode here, so there is a ceiling in code as well
    ///         as a choice in the UI.
    uint64 public constant MAX_GRANT = 30 days;

    bytes32 private constant SWAP_TYPEHASH = keccak256(
        "SarfSwap(address account,uint256 chainId,address sellToken,address buyToken,"
        "uint256 sellAmount,uint256 minBuyAmount,address target,bytes32 dataHash,"
        "uint256 nonce,uint256 deadline)"
    );

    // -------------------------------------------------------------- modifier

    /// @dev Under 7702 `address(this)` IS the user's EOA, so a self-call is
    ///      precisely "a transaction the user signed with their own wallet key".
    ///      That is the authority for granting and revoking, and the session
    ///      key can never satisfy it.
    modifier onlyAccount() {
        if (msg.sender != address(this)) revert NotSelf();
        _;
    }

    // ---------------------------------------------------------- grant / revoke

    /**
     * @notice Authorise a session key. Called by the user's own wallet.
     * @dev Overwrites any previous grant, so re-granting is also how a key is
     *      rotated. Caps are in `stable` units (USDT has 6 decimals on X Layer).
     */
    function authorize(
        address sessionKey,
        uint64 expiry,
        address router,
        address stable,
        uint128 perTradeCap,
        uint128 dailyCap,
        address[] calldata tokens
    ) external onlyAccount {
        if (sessionKey == address(0)) revert ZeroKey();
        if (expiry <= block.timestamp) revert GrantExpired();
        if (expiry > block.timestamp + MAX_GRANT) revert GrantTooLong();

        grant = Grant({
            sessionKey: sessionKey,
            expiry: expiry,
            router: router,
            stable: stable,
            perTradeCap: perTradeCap,
            dailyCap: dailyCap,
            spentToday: 0,
            dayStart: _today()
        });
        for (uint256 i; i < tokens.length; ++i) {
            allowedToken[tokens[i]] = true;
        }
        allowedToken[stable] = true;

        emit Granted(sessionKey, expiry, perTradeCap, dailyCap);
    }

    /**
     * @notice End the grant immediately. Only the user can call this, and it
     *         needs nothing from Sarf — which is what makes the arrangement
     *         revocable rather than merely time-limited.
     */
    function revoke() external onlyAccount {
        address key = grant.sessionKey;
        delete grant;
        emit Revoked(key);
    }

    // ------------------------------------------------------------- execution

    /**
     * @notice Execute one swap, authorised by the session key and bounded by
     *         the grant. Callable by anyone — the signature is the authority,
     *         so a relayer can pay the gas without gaining any power.
     * @param data Opaque router calldata. Deliberately unparsed; see the
     *             post-conditions at the end of this function, which are what
     *             actually constrain the outcome.
     */
    function executeSwap(
        address sellToken,
        address buyToken,
        uint256 sellAmount,
        uint256 minBuyAmount,
        address target,
        bytes calldata data,
        uint256 nonce,
        uint256 deadline,
        bytes calldata signature
    ) external returns (uint256 bought) {
        Grant memory g = grant;
        if (g.sessionKey == address(0)) revert NoGrant();
        if (block.timestamp > g.expiry) revert GrantExpired();
        if (block.timestamp > deadline) revert Deadline();
        if (usedNonce[nonce]) revert NonceUsed();
        if (target != g.router) revert TargetNotAllowed();
        if (!allowedToken[sellToken] || !allowedToken[buyToken]) revert TokenNotAllowed();

        // Bind the signature to this account and chain as well as to the trade,
        // so a grant on one chain cannot authorise the identical call on another.
        bytes32 digest = keccak256(
            abi.encodePacked(
                "\x19Ethereum Signed Message:\n32",
                keccak256(
                    abi.encode(
                        SWAP_TYPEHASH, address(this), block.chainid,
                        sellToken, buyToken, sellAmount, minBuyAmount,
                        target, keccak256(data), nonce, deadline
                    )
                )
            )
        );
        if (_recover(digest, signature) != g.sessionKey) revert BadSignature();
        usedNonce[nonce] = true;

        // Caps are always measured in the stable leg: a buy spends stable, a
        // sell is guaranteed to return at least minBuyAmount of it. Sizing a
        // limit in shares would mean a cap that drifts with the share price.
        uint256 counted = sellToken == g.stable
            ? sellAmount
            : (buyToken == g.stable ? minBuyAmount : 0);
        if (counted > g.perTradeCap) revert OverPerTradeCap();

        uint64 today = _today();
        uint128 spent = g.dayStart == today ? g.spentToday : 0;
        if (uint256(spent) + counted > g.dailyCap) revert OverDailyCap();
        // Safe: the line above reverts unless spent + counted <= dailyCap,
        // and dailyCap is itself a uint128.
        // forge-lint: disable-next-line(unsafe-typecast)
        grant.spentToday = uint128(spent + counted);
        grant.dayStart = today;

        uint256 sellBefore = _balanceOf(sellToken, address(this));
        uint256 buyBefore = _balanceOf(buyToken, address(this));

        // Exact allowance, zeroed straight after, so no standing approval can
        // be left behind for the router or anyone who can call it later.
        _approve(sellToken, target, sellAmount);
        (bool ok, ) = target.call{value: 0}(data);
        if (!ok) revert SwapFailed();
        _approve(sellToken, target, 0);

        // The real constraints. Everything above decides whether the call is
        // allowed to be attempted; these two decide what it is allowed to have
        // done, whatever the calldata actually contained.
        uint256 sellAfter = _balanceOf(sellToken, address(this));
        uint256 buyAfter = _balanceOf(buyToken, address(this));
        if (sellBefore - sellAfter > sellAmount) revert SoldTooMuch();
        bought = buyAfter - buyBefore;
        if (bought < minBuyAmount) revert ReceivedTooLittle();

        emit Swapped(sellToken, buyToken, sellAmount, bought, counted);
    }

    // -------------------------------------------------------------- helpers

    /// @notice Remaining stable-denominated allowance for the current UTC day.
    function remainingToday() external view returns (uint256) {
        Grant memory g = grant;
        if (g.sessionKey == address(0) || block.timestamp > g.expiry) return 0;
        uint128 spent = g.dayStart == _today() ? g.spentToday : 0;
        return g.dailyCap > spent ? g.dailyCap - spent : 0;
    }

    function _today() private view returns (uint64) {
        return uint64(block.timestamp / 1 days);
    }

    function _balanceOf(address token, address who) private view returns (uint256) {
        (bool ok, bytes memory out) =
            token.staticcall(abi.encodeWithSelector(0x70a08231, who)); // balanceOf
        if (!ok || out.length < 32) revert SwapFailed();
        return abi.decode(out, (uint256));
    }

    /// @dev Tolerates the non-standard ERC-20s that return nothing from
    ///      approve. USDT is the canonical offender and is the stable leg of
    ///      every trade here, so this cannot be skipped.
    function _approve(address token, address spender, uint256 value) private {
        (bool ok, bytes memory out) =
            token.call(abi.encodeWithSelector(0x095ea7b3, spender, value)); // approve
        if (!ok || (out.length != 0 && !abi.decode(out, (bool)))) revert SwapFailed();
    }

    function _recover(bytes32 digest, bytes calldata sig) private pure returns (address) {
        if (sig.length != 65) revert BadSignature();
        bytes32 r = bytes32(sig[0:32]);
        bytes32 s = bytes32(sig[32:64]);
        uint8 v = uint8(sig[64]);
        // Reject the upper half of the curve order: without this both s and
        // (n - s) verify, so every authorised swap would have a second valid
        // signature and the nonce would be the only thing preventing replay.
        if (uint256(s) > 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0) {
            revert BadSignature();
        }
        if (v < 27) v += 27;
        address signer = ecrecover(digest, v, r, s);
        if (signer == address(0)) revert BadSignature();
        return signer;
    }

    /// @dev Accept OKB so gas top-ups to the account still work while it is
    ///      delegated. Nothing in this contract can send it back out.
    receive() external payable {}
}

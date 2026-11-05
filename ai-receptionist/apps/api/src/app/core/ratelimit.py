"""Fixed-window counters in Redis, for throttling abuse-prone endpoints.

Atomic by construction: the increment, the expiry and the TTL read happen in
one Lua script, so a crash between INCR and EXPIRE can never leave a counter
that never resets (which for a login throttle would be a permanent lockout).
"""

from redis.asyncio import Redis

_HIT = """
local n = redis.call('INCR', KEYS[1])
local ttl = redis.call('TTL', KEYS[1])
if n == 1 or ttl < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
    ttl = tonumber(ARGV[1])
end
return {n, ttl}
"""

_REFUND = """
local n = tonumber(redis.call('GET', KEYS[1]) or '0')
if n > 0 then return redis.call('DECR', KEYS[1]) end
return 0
"""


async def hit(redis: Redis, key: str, window_seconds: int) -> tuple[int, int]:
    """Count one event. Returns (events in this window including this one,
    seconds until the window resets)."""
    count, ttl = await redis.eval(_HIT, 1, key, window_seconds)
    return int(count), int(ttl)


async def refund(redis: Redis, key: str) -> None:
    """Undo one `hit` — for events that turned out to be legitimate."""
    await redis.eval(_REFUND, 1, key)


async def reset(redis: Redis, *keys: str) -> None:
    if keys:
        await redis.delete(*keys)

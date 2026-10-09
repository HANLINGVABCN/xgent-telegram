"""Shared, validated Web typography stored in SQLite configuration."""
import json

FONT_SETTINGS = {
    'web_message_font_size': (14, 12, 24),
    'web_ui_font_size': (13, 12, 20),
    'web_terminal_font_size': (13, 10, 24),
}


def validate_font(key, value):
    default, minimum, maximum = FONT_SETTINGS[key]
    if type(value) not in (int, float) or not minimum <= value <= maximum or int(value) != value:
        raise ValueError(f'字号必须是 {minimum}–{maximum} 之间的整数。')
    return int(value)


async def read_appearance(db):
    conn = await db._get_conn()
    keys = [*FONT_SETTINGS, 'web_settings_revision']
    cursor = await conn.execute('SELECT key,value FROM config WHERE key IN (' + ','.join('?' for _ in keys) + ')', keys)
    raw = {row['key']: json.loads(row['value']) for row in await cursor.fetchall()}
    values = {}
    for key, (default, _, _) in FONT_SETTINGS.items():
        try:
            values[key] = validate_font(key, raw.get(key, default))
        except (ValueError, TypeError, OverflowError):
            values[key] = default
    return {'revision': int(raw.get('web_settings_revision') or 0), 'values': values}


async def write_appearance(db, key, value):
    value = validate_font(key, value)
    async with db._transaction() as conn:
        cursor = await conn.execute("SELECT value FROM config WHERE key='web_settings_revision'")
        row = await cursor.fetchone()
        revision = int(json.loads(row['value'])) + 1 if row else 1
        for field, result in ((key,value),('web_settings_revision',revision)):
            await conn.execute('INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (field,json.dumps(result)))
    db._config_cache.pop(key, None)
    return await read_appearance(db)

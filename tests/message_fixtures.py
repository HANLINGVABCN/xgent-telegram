"""Current typed records for tests that deliberately bypass application writers."""
import json

from xgent_app.message_policy import stamp_record_metadata


def typed_record(record):
    result = dict(record)
    result['metadata'] = stamp_record_metadata(result['msg_type'], result.get('role'), result.get('metadata'))
    result['message_policy'] = result['metadata']['message_policy']
    return result


def stamp_fixture_messages(connection, conversation_id):
    rows = connection.execute('SELECT id,msg_type,role,metadata FROM global_messages WHERE session_id=?',
                              (conversation_id,)).fetchall()
    for key, kind, role, metadata in rows:
        connection.execute('UPDATE global_messages SET metadata=? WHERE id=?',
                           (json.dumps(stamp_record_metadata(kind, role, metadata)), key))


def current_history_message(record, *args, **kwargs):
    from xgent_app.web_history import build_history_message
    return build_history_message(typed_record(record), *args, **kwargs)

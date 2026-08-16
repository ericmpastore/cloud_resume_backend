import unittest

import json
import os

import functions_framework
import pymysql

_connection = None


def get_connection():
    global _connection
    if _connection is None or not _connection.open:
        _connection = pymysql.connect(
            # Cloud Run mounts the Cloud SQL connection at this Unix socket path
            # once the instance is attached under the function's "Connections" tab.
            unix_socket=f"/cloudsql/{os.environ['INSTANCE_CONNECTION_NAME']}",
            user=os.environ['DB_USER'],
            password=os.environ['DB_PASS'],
            database=os.environ['DB_NAME'],
            autocommit=True,
        )
    return _connection


@functions_framework.http
def write_number(request):
    # CORS: allow the static site's origin (your load-balanced domain) to call this function.
    headers = {
        'Access-Control-Allow-Origin': os.environ.get('ALLOWED_ORIGIN', '*'),
        'Access-Control-Allow-Methods': 'POST, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Content-Type': 'application/json',
    }

    if request.method == 'OPTIONS':
        # Preflight request
        return ('', 204, headers)

    if request.method != 'POST':
        return (json.dumps({'error': 'Only POST is supported.'}), 405, headers)

    body = request.get_json(silent=True) or {}
    try:
        value = float(body.get('value'))
    except (TypeError, ValueError):
        return (
            json.dumps({'error': 'Request body must include a numeric "value" field.'}),
            400,
            headers,
        )

    try:
        conn = get_connection()
        with conn.cursor() as cursor:
            cursor.execute('INSERT INTO numbers (value) VALUES (%s)', (value,))
            insert_id = cursor.lastrowid
        return (json.dumps({'success': True, 'insertId': insert_id, 'value': value}), 200, headers)
    except Exception as err:
        print(f'DB insert failed: {err}')
        return (json.dumps({'error': 'Failed to write to database.'}), 500, headers)
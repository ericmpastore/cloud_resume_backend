# Code for backend serverless function, EPastore 07/24/2026
import os
from flask import Flask, request, jsonify
import sqlalchemy
from google.cloud.sql.connector import Connector, IPTypes

app = Flask(__name__)

# Initialize the Cloud SQL Connector, EPastore 07/25/2026
connector = Connector()

# Define environment variables, EPastore 07/25/2026
def getconn():
    conn = connector.connect(
        os.environ["INSTANCE_CONNECTION_NAME"],
        "pymysql",
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
        db=os.environ["DB_NAME"]
    )
    return conn

# 2. Create the SQLAlchemy connection pool
pool = sqlalchemy.create_engine(
    "mysql+pymysql://",
    creator=getconn,
)

@app.route("/write", methods=["POST"])
def write_to_db():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    # Example: Inserting a visitor name or timestamp
    # Adjust 'visitor_name' to match your JavaScript payload
    visitor_name = data.get("name", "Anonymous")

    try:
        with pool.connect() as db_conn:
            # Replace 'resume_views' with the final table name
            insert_stmt = sqlalchemy.text(
                "INSERT INTO resume_views (visitor_name) VALUES (:visitor_name)"
            )
            db_conn.execute(insert_stmt, parameters={"visitor_name": visitor_name})
            db_conn.commit()
        return jsonify({"status": "success"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
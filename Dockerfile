FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run injects PORT at runtime; functions-framework listens on it.
ENV PORT=8080
# Must match the entry-point function name in main.py (section 2).
ENV FUNCTION_TARGET=write_number

CMD exec functions-framework --target=${FUNCTION_TARGET} --port=${PORT}
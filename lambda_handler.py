"""
AWS Lambda entry point для Request Classifier.

Спрацьовує на подію S3 PutObject (завантаження CSV у вхідний бакет):
    1. Завантажує CSV з S3 у /tmp (єдине записуване місце в Lambda).
    2. Обробляє його тим самим кодом, що й локальний запуск (main.py):
       load_requests -> process_all_async -> save_output/build_report.
       Запити обробляються паралельно (ліміт - змінна MAX_CONCURRENCY).
    3. Завантажує output.json і report.md назад у вихідний S3-бакет.
    4. Надсилає підсумок і запити по відділах у Telegram (якщо задано
       TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID).

GEMINI_API_KEY передається через Lambda environment variables
(Configuration -> Environment variables), а не через .env файл.
"""

import asyncio
import logging
import os

import boto3

from classifier import classify_request  # noqa: F401 (потрібен для process_all)
from main import build_report, load_requests, process_all_async, save_output
from telegram_notifier import notify_results

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logging.getLogger().setLevel(logging.INFO)  # Lambda вже має handler, тож basicConfig рівень не ставить
logger = logging.getLogger(__name__)

s3 = boto3.client("s3")

OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "request-classifier-output-677296949589")

LOCAL_INPUT_PATH = "/tmp/input_requests.csv"
LOCAL_OUTPUT_JSON = "/tmp/output.json"
LOCAL_REPORT_MD = "/tmp/report.md"


def lambda_handler(event, context):
    """Точка входу Lambda. event містить S3-подію (Records[0].s3.bucket/object)."""
    record = event["Records"][0]
    input_bucket = record["s3"]["bucket"]["name"]
    input_key = record["s3"]["object"]["key"]

    logger.info(f"Отримано подію: s3://{input_bucket}/{input_key}")

    # 1. Завантажуємо CSV із S3 у /tmp
    s3.download_file(input_bucket, input_key, LOCAL_INPUT_PATH)
    logger.info(f"CSV завантажено у {LOCAL_INPUT_PATH}")

    # 2. Обробляємо тим самим кодом, що й локально
    requests_list = load_requests(LOCAL_INPUT_PATH)
    logger.info(f"Знайдено {len(requests_list)} запитів")

    results = asyncio.run(process_all_async(requests_list))

    save_output(results, LOCAL_OUTPUT_JSON)
    build_report(results, LOCAL_REPORT_MD)

    # 3. Завантажуємо результати назад у S3 (output-бакет)
    base_name = os.path.splitext(os.path.basename(input_key))[0]
    output_json_key = f"results/{base_name}_output.json"
    report_key = f"results/{base_name}_report.md"

    s3.upload_file(LOCAL_OUTPUT_JSON, OUTPUT_BUCKET, output_json_key)
    s3.upload_file(LOCAL_REPORT_MD, OUTPUT_BUCKET, report_key)

    logger.info(f"Результати завантажено: s3://{OUTPUT_BUCKET}/{output_json_key}")

    # 4. Сповіщення в Telegram (необов'язково; збій не ламає обробку)
    notify_results(results, os.path.basename(input_key))

    return {
        "statusCode": 200,
        "body": f"Оброблено {len(results)} запитів. Результати: {output_json_key}, {report_key}",
    }

import multiprocessing
import os

bind = f"127.0.0.1:{os.getenv('PORT', '5000')}"
workers = 1
threads = 1
timeout = int(os.getenv('GUNICORN_TIMEOUT', '120'))
keepalive = 5
accesslog = '-'
errorlog = '-'
capture_output = True

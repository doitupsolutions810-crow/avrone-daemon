FROM python:3.12-slim
WORKDIR /app
COPY cycle_link.py evidence.py verify.sh ./
COPY tests/ tests/
RUN chmod +x verify.sh
CMD ["bash", "verify.sh"]

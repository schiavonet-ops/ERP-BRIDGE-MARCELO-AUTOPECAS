FROM python:3.11-slim
RUN apt-get update && \
    apt-get install -y libfbclient2 cron && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p data
RUN echo -e "*/15 * * * * cd /app && PYTHONPATH=/app /usr/local/bin/python3 -m core.sync_worker delta >> /app/data/cron.log 2>&1\n*/15 * * * * cd /app && PYTHONPATH=/app /usr/local/bin/python3 -m core.nf_sync >> /app/data/nf_sync.log 2>&1" | crontab -
ENV PYTHONPATH=/app
ENV FB_DLL=/usr/lib/x86_64-linux-gnu/libfbclient.so.2
ENV FB_HOST=168.205.222.164
ENV FB_PORT=3050
ENV FB_DATABASE=C:/Enfoque/ERP/Data/erp.fdb
ENV FB_USER=SYSDBA
ENV FB_PASSWORD=masterkey
ENV FB_CHARSET=WIN1252
EXPOSE 8000
COPY start.sh /start.sh
RUN chmod +x /start.sh
CMD ["/start.sh"]

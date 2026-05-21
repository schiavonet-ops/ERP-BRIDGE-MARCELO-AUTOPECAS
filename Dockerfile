FROM python:3.11-slim

# Instala Firebird client para Linux
RUN apt-get update && \
    apt-get install -y libfbclient2 cron && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instala dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia código
COPY . .

# Cria pastas necessárias
RUN mkdir -p data

# Configura cron para sync delta a cada 15 minutos
RUN echo "*/15 * * * * cd /app && python core/sync_worker.py delta >> /app/data/cron.log 2>&1" | crontab -

# Script de inicialização
RUN echo '#!/bin/bash\n\
# Inicia cron em background\n\
service cron start\n\
# Sync inicial se banco estiver vazio\n\
python -c "from core.local_db import total_produtos; t=total_produtos(); exit(0 if t[\"total\"]>0 else 1)" 2>/dev/null || python core/sync_worker.py completo\n\
# Sobe a API\n\
exec uvicorn api.main:app --host 0.0.0.0 --port 8000\n\
' > /app/start.sh && chmod +x /app/start.sh

ENV PYTHONPATH=/app
ENV FB_DLL=/usr/lib/x86_64-linux-gnu/libfbclient.so.2
ENV FB_HOST=168.205.222.164
ENV FB_PORT=3050
ENV FB_DATABASE=C:/Enfoque/ERP/Data/erp.fdb
ENV FB_USER=SYSDBA
ENV FB_PASSWORD=masterkey
ENV FB_CHARSET=WIN1252

EXPOSE 8000

CMD ["/app/start.sh"]

# LoL Noob Helper para correr no portfolio, dentro de um path (ex.: /lol/).
# No servidor não há cliente do LoL: funcionam as partes que usam só a API da Riot
# (pesquisa de contas, perfis, histórico e análise de jogos). Precisa de RIOT_API_KEY.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY coach ./coach
COPY static ./static
ENV PORT=8080
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "coach.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]

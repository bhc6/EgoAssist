FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY egoassist ./egoassist
COPY run_egoassist.py run_egolife_benchmark.py run_interaction_benchmark.py ./

RUN pip install --no-cache-dir .

EXPOSE 8770
CMD ["egoassist", "--host", "0.0.0.0", "--port", "8770"]

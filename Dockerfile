# Stage 1: download the Kaggle datasets and build the SQLite store.
# The processed store is baked into the final image at build time, so the
# running container never touches the network for data.
FROM python:3.12-slim AS build
WORKDIR /build
RUN pip install --no-cache-dir kagglehub pandas pyarrow numpy
COPY pmbt/ pmbt/
RUN python -m pmbt.ingest --download --raw data_raw --db /build/data/store.db

# Stage 2: runtime image
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastmcp>=3,<4" uvicorn
COPY pmbt/ pmbt/
COPY --from=build /build/data/store.db data/store.db
ENV PMBT_DB=/app/data/store.db
EXPOSE 8000
CMD ["python", "-m", "pmbt.server"]

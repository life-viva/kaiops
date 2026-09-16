FROM python:3.11-slim
WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
COPY app/ .
# 项目文档（踩坑实录等）打包进镜像，作为 RAG 知识库——应用"读过"自己项目的文档
COPY docs/ ./knowledge
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

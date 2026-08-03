FROM mcr.microsoft.com/dotnet/sdk:8.0-alpine@sha256:5f12aa62868b69dcb41de9cd7f8759822f7d1f56c3b31908048ad65df0981e67
WORKDIR /app
COPY . /app
CMD ["sh", "-lc", "dotnet new console -n App -o . --force >/dev/null && dotnet run"]

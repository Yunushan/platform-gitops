FROM mcr.microsoft.com/dotnet/sdk:8.0-alpine
WORKDIR /app
COPY . /app
CMD ["sh", "-lc", "dotnet new console -n App -o . --force >/dev/null && dotnet run"]

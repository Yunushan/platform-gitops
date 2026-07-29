FROM gcc:14@sha256:1ea81e094f614fd2ed066316651dbac8eecb4d36add2ddd8a26151374c85c52c
WORKDIR /app
COPY . /app
CMD ["sh", "-lc", "g++ main.cpp -o app && ./app"]

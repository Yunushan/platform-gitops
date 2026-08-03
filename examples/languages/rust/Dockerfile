FROM rust:1-alpine@sha256:3c38f3f82c2f3d73da3b38e18d279393a04cb43ddded0e35088a8c3324d40900
WORKDIR /app
COPY . /app
CMD ["sh", "-lc", "rustc main.rs && ./main"]

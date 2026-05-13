# C# service scaffold

Minimal C# starter for CI testing and containerization.

## Test

```bash
dotnet new console -n App -o . --force >/dev/null && dotnet run
```

## CI/CD

Use Woodpecker, GitHub Actions, GitLab CI, Gitea Actions, or Forgejo Actions from `examples/service-template` as the base pipeline.

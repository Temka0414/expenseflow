# Two stages: the large SDK image compiles the app, and only the published output is copied into
# the small ASP.NET Core runtime image that actually runs (no SDK, no source code).
# Both images match your PC's processor, x64, which is also what the Fargate task runs on.
# Building on an ARM machine (or for ARM Fargate) would need a cross-build.
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src
# Restore from the project file alone first: Docker caches this layer, so a rebuild after a code
# change does not download the NuGet packages again.
COPY CompanyApp.csproj .
RUN dotnet restore
COPY . .
RUN dotnet publish -c Release -o /app --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:10.0
WORKDIR /app
COPY --from=build /app .
# The aspnet image has a built-in non-root user (APP_UID): a compromised app is not root in the
# container. Such a user may not open ports below 1024, which is why the image's default port,
# set by ASPNETCORE_HTTP_PORTS, is 8080 rather than 80.
EXPOSE 8080
USER $APP_UID
ENTRYPOINT ["dotnet", "CompanyApp.dll"]

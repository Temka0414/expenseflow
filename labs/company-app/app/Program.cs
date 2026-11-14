// CompanyApp: a small IT equipment register (Razor Pages + EF Core + PostgreSQL).
// The same container image runs on your PC with Docker Compose and on AWS (ECS Fargate + RDS).
using CompanyApp.Data;
using Microsoft.AspNetCore.Mvc;
using Microsoft.EntityFrameworkCore;

var builder = WebApplication.CreateBuilder(args);

// Antiforgery tokens are encrypted with a key that each container creates for itself. Behind the
// load balancer a form served by one container and posted to the other would fail with 400. The
// app has no sign-in, so a forged form could do nothing a visitor cannot do anyway; an app with
// sign-in keeps the check and shares the key between containers (ASP.NET Core Data Protection).
builder.Services.AddRazorPages(options =>
    options.Conventions.ConfigureFilter(new IgnoreAntiforgeryTokenAttribute()));

// The connection string is built inside the lambda, that is whenever a DbContext is created, so the
// tests can start the app without DB_* variables (`dotnet ef` uses DesignTimeDbContextFactory). A
// real start still fails fast: the migration below creates the first DbContext before the app
// serves a single request.
builder.Services.AddDbContext<AppDbContext>(options =>
    options.UseNpgsql(DatabaseSettings.BuildConnectionString(builder.Configuration)));

var app = builder.Build();

// Applying migrations at startup is fine for a small lab (EF Core takes a lock, so two containers
// starting together do not both apply them). Production runs them as a separate deployment step:
// a failing migration then stops the release instead of every container start, and the app's
// database user does not need the right to change tables.
// MIGRATE_ON_STARTUP=false exists only for the tests, which run without a database.
if (app.Configuration.GetValue("MIGRATE_ON_STARTUP", true))
{
    using var scope = app.Services.CreateScope();
    scope.ServiceProvider.GetRequiredService<AppDbContext>().Database.Migrate();
}

// No HTTPS redirection: the lab's load balancer listens on plain HTTP (a certificate needs a
// domain name of your own).
app.UseStaticFiles();
app.MapRazorPages();

// The load balancer's health check. It deliberately does not query the database: during a
// database outage every container would fail it, and ECS would replace them all for nothing.
app.MapGet("/health", () => "Healthy");

app.Run();

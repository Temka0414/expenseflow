using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Design;

namespace CompanyApp.Data;

/// <summary>Lets `dotnet ef migrations add` run without any DB_* variables.</summary>
/// <remarks>
/// Creating a migration only compares the model with Migrations/AppDbContextModelSnapshot.cs and
/// never connects to a database, so no connection string is needed. The app applies migrations
/// itself at startup.
/// </remarks>
public class DesignTimeDbContextFactory : IDesignTimeDbContextFactory<AppDbContext>
{
    public AppDbContext CreateDbContext(string[] args) =>
        new(new DbContextOptionsBuilder<AppDbContext>().UseNpgsql().Options);
}

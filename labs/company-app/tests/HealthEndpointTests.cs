using Microsoft.AspNetCore.Mvc.Testing;

namespace CompanyApp.Tests;

public class HealthEndpointTests
{
    [Fact]
    public async Task Health_returns_Healthy_without_a_database()
    {
        // No DB_* settings and no migration: if /health touched the database, this would fail.
        using var factory = new WebApplicationFactory<Program>()
            .WithWebHostBuilder(host => host.UseSetting("MIGRATE_ON_STARTUP", "false"));
        using var client = factory.CreateClient();

        var response = await client.GetAsync("/health");

        response.EnsureSuccessStatusCode();
        Assert.Equal("Healthy", await response.Content.ReadAsStringAsync());
    }
}

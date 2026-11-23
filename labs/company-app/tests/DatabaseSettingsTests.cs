using CompanyApp.Data;
using Microsoft.Extensions.Configuration;
using Npgsql;

namespace CompanyApp.Tests;

public class DatabaseSettingsTests
{
    private static Dictionary<string, string?> AllRequired() => new()
    {
        ["DB_HOST"] = "db.example.internal",
        ["DB_NAME"] = "companyapp",
        ["DB_USER"] = "app_user",
        ["DB_PASSWORD"] = "secret",
    };

    private static NpgsqlConnectionStringBuilder Build(Dictionary<string, string?> variables)
    {
        var config = new ConfigurationBuilder().AddInMemoryCollection(variables).Build();
        return new NpgsqlConnectionStringBuilder(DatabaseSettings.BuildConnectionString(config));
    }

    [Fact]
    public void Uses_the_variables_with_port_5432_and_ssl_prefer_by_default()
    {
        var result = Build(AllRequired());

        Assert.Equal("db.example.internal", result.Host);
        Assert.Equal(5432, result.Port);
        Assert.Equal("companyapp", result.Database);
        Assert.Equal("app_user", result.Username);
        Assert.Equal("secret", result.Password);
        Assert.Equal(SslMode.Prefer, result.SslMode);
    }

    [Fact]
    public void Uses_DB_PORT_when_set()
    {
        var variables = AllRequired();
        variables["DB_PORT"] = "6543";

        Assert.Equal(6543, Build(variables).Port);
    }

    [Theory]
    [InlineData("DB_HOST")]
    [InlineData("DB_NAME")]
    [InlineData("DB_USER")]
    [InlineData("DB_PASSWORD")]
    public void A_missing_variable_fails_with_its_name(string name)
    {
        var variables = AllRequired();
        variables.Remove(name);

        var error = Assert.Throws<InvalidOperationException>(() => Build(variables));
        Assert.Equal($"The environment variable {name} is not set.", error.Message);
    }
}

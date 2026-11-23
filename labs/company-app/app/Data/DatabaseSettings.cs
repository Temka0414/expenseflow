using Npgsql;

namespace CompanyApp.Data;

/// <summary>Turns the DB_* environment variables into a PostgreSQL connection string.</summary>
/// <remarks>
/// Why environment variables and not appsettings.json: the same container image runs on your PC
/// (compose.yaml sets them) and on ECS Fargate (the task definition sets them, and ECS reads the
/// user name and password from Secrets Manager). Only the environment changes, never the image.
/// </remarks>
public static class DatabaseSettings
{
    public static string BuildConnectionString(IConfiguration config)
    {
        var builder = new NpgsqlConnectionStringBuilder
        {
            Host = Required(config, "DB_HOST"),
            Port = config.GetValue("DB_PORT", 5432),
            Database = Required(config, "DB_NAME"),
            Username = Required(config, "DB_USER"),
            Password = Required(config, "DB_PASSWORD"),
            // Prefer = encrypt when the server offers TLS, plain text when it does not. One setting
            // then works with the local postgres container (no TLS) and with RDS (PostgreSQL 15
            // and later on RDS refuse connections without TLS). Prefer does not check who answers:
            // production should use SslMode.VerifyFull with the RDS certificate bundle.
            SslMode = SslMode.Prefer,
        };
        return builder.ConnectionString;
    }

    // Failing with the variable's name beats Npgsql's later, vaguer "host not found" or
    // "password authentication failed".
    private static string Required(IConfiguration config, string name)
    {
        var value = config[name];
        if (string.IsNullOrWhiteSpace(value))
        {
            throw new InvalidOperationException($"The environment variable {name} is not set.");
        }
        return value;
    }
}

using System.ComponentModel.DataAnnotations;
using CompanyApp.Data;

namespace CompanyApp.Tests;

public class AssetValidationTests
{
    // The data-annotation check behind ModelState.IsValid when a form is posted.
    private static List<string> InvalidMembers(Asset asset)
    {
        var results = new List<ValidationResult>();
        var context = new ValidationContext(asset);
        Validator.TryValidateObject(asset, context, results, validateAllProperties: true);
        return results.SelectMany(r => r.MemberNames).ToList();
    }

    [Fact]
    public void Name_and_category_are_enough()
    {
        Assert.Empty(InvalidMembers(new Asset { Name = "ThinkPad T14", Category = "Laptop" }));
    }

    [Fact]
    public void Name_and_category_are_required()
    {
        Assert.Equal(["Name", "Category"], InvalidMembers(new Asset()));
    }

    [Fact]
    public void Too_long_values_are_rejected()
    {
        var asset = new Asset
        {
            Name = new string('n', 101),
            Category = new string('c', 51),
            SerialNumber = new string('s', 101),
            AssignedTo = new string('a', 101),
        };

        Assert.Equal(["Name", "Category", "SerialNumber", "AssignedTo"], InvalidMembers(asset));
    }
}

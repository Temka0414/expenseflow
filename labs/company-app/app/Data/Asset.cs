using System.ComponentModel.DataAnnotations;
using Microsoft.AspNetCore.Mvc.ModelBinding;

namespace CompanyApp.Data;

/// <summary>One piece of IT equipment: a laptop, a monitor, a phone...</summary>
/// <remarks>
/// The attributes do double duty: Razor Pages validates the forms with them and EF Core turns the
/// maximum lengths into varchar(n) columns, so the form and the database cannot disagree.
/// </remarks>
public class Asset
{
    // Never read from a posted form: new rows get their id from the database, and the Edit and
    // Delete pages take it from the URL. Otherwise a crafted form could pick the id of a new row.
    [BindNever]
    public int Id { get; set; }

    [Required, StringLength(100)]
    public string Name { get; set; } = "";

    [Required, StringLength(50)]
    public string Category { get; set; } = "";

    [StringLength(100), Display(Name = "Serial number")]
    public string? SerialNumber { get; set; }

    [StringLength(100), Display(Name = "Assigned to")]
    public string? AssignedTo { get; set; }

    [Display(Name = "Purchased on")]
    public DateOnly? PurchasedOn { get; set; }
}

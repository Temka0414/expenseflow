using CompanyApp.Data;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.RazorPages;

namespace CompanyApp.Pages;

public class CreateModel(AppDbContext db) : PageModel
{
    [BindProperty]
    public Asset Asset { get; set; } = new();

    public async Task<IActionResult> OnPostAsync()
    {
        if (!ModelState.IsValid)
        {
            return Page();
        }

        db.Assets.Add(Asset);
        await db.SaveChangesAsync();
        return RedirectToPage("Index");
    }
}

using CompanyApp.Data;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.RazorPages;
using Microsoft.EntityFrameworkCore;

namespace CompanyApp.Pages;

public class EditModel(AppDbContext db) : PageModel
{
    [BindProperty]
    public Asset Asset { get; set; } = new();

    public async Task<IActionResult> OnGetAsync(int id)
    {
        var asset = await db.Assets.FindAsync(id);
        if (asset is null)
        {
            return NotFound();
        }

        Asset = asset;
        return Page();
    }

    public async Task<IActionResult> OnPostAsync(int id)
    {
        if (!ModelState.IsValid)
        {
            return Page();
        }

        // Deleted in another tab meanwhile: without this check EF Core updates 0 rows and throws (500).
        if (!await db.Assets.AnyAsync(a => a.Id == id))
        {
            return NotFound();
        }

        Asset.Id = id;
        db.Assets.Update(Asset);
        await db.SaveChangesAsync();
        return RedirectToPage("Index");
    }
}

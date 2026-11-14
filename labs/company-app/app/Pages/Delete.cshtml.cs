using CompanyApp.Data;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.RazorPages;
using Microsoft.EntityFrameworkCore;

namespace CompanyApp.Pages;

public class DeleteModel(AppDbContext db) : PageModel
{
    public Asset Asset { get; private set; } = new();

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
        await db.Assets.Where(a => a.Id == id).ExecuteDeleteAsync();
        return RedirectToPage("Index");
    }
}

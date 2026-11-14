using CompanyApp.Data;
using Microsoft.AspNetCore.Mvc.RazorPages;
using Microsoft.EntityFrameworkCore;

namespace CompanyApp.Pages;

public class IndexModel(AppDbContext db) : PageModel
{
    public List<Asset> Assets { get; private set; } = [];

    public async Task OnGetAsync()
    {
        Assets = await db.Assets.OrderBy(a => a.Name).ToListAsync();
    }
}

import discord


def build_pages(items, per_page=15, max_chars=3800, bullet="- "):
    pages, current, size = [], [], 0
    for item in items:
        line = f"{bullet}{item}"[:max_chars]
        if current and (len(current) >= per_page or size + len(line) + 1 > max_chars):
            pages.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        pages.append("\n".join(current))
    return pages


class PagedEmbed(discord.ui.View):
    def __init__(self, author_id, title, pages, color, total, noun="item(s)"):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.title = title
        self.pages = pages
        self.color = color
        self.total = total
        self.noun = noun
        self.page = 0
        self.message = None
        self._sync()

    def make_embed(self):
        embed = discord.Embed(title=self.title, description=self.pages[self.page], color=self.color)
        embed.set_footer(text=f"Page {self.page + 1}/{len(self.pages)} • {self.total} {self.noun}")
        return embed

    def _sync(self):
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran this command can flip pages.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.page -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.page += 1
        self._sync()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def send_paged(ctx, title, items, color, noun="item(s)", per_page=15, ephemeral=False):
    pages = build_pages(items, per_page=per_page)
    view = PagedEmbed(ctx.author.id, title, pages, color, len(items), noun)
    embed = view.make_embed()

    kwargs = {"embed": embed}
    if len(pages) > 1:
        kwargs["view"] = view

    if isinstance(ctx, discord.ApplicationContext):
        sent = await ctx.respond(ephemeral=ephemeral, **kwargs)
        if isinstance(sent, discord.Interaction):
            sent = await sent.original_response()
    else:
        sent = await ctx.send(**kwargs)

    if len(pages) > 1:
        view.message = sent
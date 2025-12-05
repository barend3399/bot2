import discord
from discord.ext import commands, tasks
import lyricsgenius
import os
from pymongo import MongoClient
from datetime import datetime
from tabulate import tabulate
import asyncio

# --- CONFIGURATIE (Haal deze uit Environment Variables voor veiligheid op Render) ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GENIUS_TOKEN = os.getenv("GENIUS_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

# --- SETUP GENIUS & DISCORD ---
genius = lyricsgenius.Genius(GENIUS_TOKEN)
genius.verbose = False # Minder spam in de console
genius.remove_section_headers = True

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- SETUP DATABASE (MongoDB) ---
cluster = MongoClient(MONGO_URI)
db = cluster["discord_bot_db"]
users_collection = db["users"]

# --- FUNCTIES VOOR CREDITS ---

def get_max_credits(member):
    """Bepaalt max credits op basis van rol."""
    role_names = [role.name for role in member.roles]
    if "Premium" in role_names:
        return 100
    elif "Lite" in role_names:
        return 30
    return 0 # Geen rol = 0 credits

def check_monthly_reset(user_data, max_credits):
    """Reset credits als we in een nieuwe maand zijn."""
    current_month = datetime.now().month
    last_reset = user_data.get("last_reset_month")
    
    if last_reset != current_month:
        # Het is een nieuwe maand, reset credits naar max (of vul aan)
        # Hier zetten we het hard op de limiet (bijv. weer terug naar 30 of 100)
        return max_credits, current_month
    return user_data["credits"], last_reset

def process_credits(user_id, member):
    """Behandelt de credit logica (checken, resetten, aftrekken)."""
    max_credits = get_max_credits(member)
    
    if max_credits == 0:
        return False, "Je hebt geen 'Lite' of 'Premium' rol."

    user_data = users_collection.find_one({"_id": user_id})

    if not user_data:
        # Nieuwe gebruiker in DB
        user_data = {"_id": user_id, "credits": max_credits, "last_reset_month": datetime.now().month}
        users_collection.insert_one(user_data)
    
    # Check voor maandelijkse reset
    current_credits, month_check = check_monthly_reset(user_data, max_credits)
    
    # Update DB als maand veranderd is
    if month_check != user_data["last_reset_month"]:
        users_collection.update_one({"_id": user_id}, {"$set": {"credits": current_credits, "last_reset_month": month_check}})

    if current_credits > 0:
        # Credits aftrekken
        users_collection.update_one({"_id": user_id}, {"$inc": {"credits": -1}})
        return True, current_credits - 1
    else:
        return False, "Je credits voor deze maand zijn op!"

# --- HET COMMANDO ---

@bot.command(name="album")
async def search_album(ctx, *, album_name: str):
    # 1. Check Credits
    allowed, message = process_credits(ctx.author.id, ctx.author)
    
    if not allowed:
        await ctx.send(f"❌ {message}")
        return
    
    remaining_credits = message
    await ctx.send(f"🔍 Album **'{album_name}'** wordt gezocht... (Credits over: {remaining_credits})\n*Dit kan even duren omdat ik alle nummers moet scannen.*")

    try:
        # 2. Zoek Album via Genius
        # We runnen dit in een executor om de bot niet te blokkeren tijdens het laden
        loop = asyncio.get_event_loop()
        album = await loop.run_in_executor(None, lambda: genius.search_album(album_name))

        if not album:
            await ctx.send("❌ Album niet gevonden op Genius.")
            # Optioneel: Credits teruggeven als het faalt
            users_collection.update_one({"_id": ctx.author.id}, {"$inc": {"credits": 1}})
            return

        data_rows = []
        
        # 3. Loop door de nummers en zoek producers
        # Let op: De Genius API kan traag zijn als een album veel nummers heeft.
        for track in album.tracks:
            song = genius.search_song(track.song.title, album.artist.name)
            
            if song:
                producers_list = song.producer_artists
                
                if not producers_list:
                    data_rows.append([song.title[:20], "Onbekend", "-"])
                else:
                    for producer in producers_list:
                        # Probeer IG link te vinden. Genius API geeft dit niet altijd direct terug in de search
                        # We doen een 'best effort'
                        prod_name = producer['name']
                        ig_link = "-"
                        
                        # Soms staat de URL in de metadata, maar vaak moet je de artist apart fetchen
                        # Om de bot niet extreem traag te maken, gebruiken we de standaard URL als placeholder
                        # of de API url als die beschikbaar is. 
                        # Voor echte IG links moet je vaak nog een extra API call doen per producer:
                        try:
                            # Let op: Dit vertraagt het proces aanzienlijk per nummer!
                            # Zet dit uit als het te langzaam gaat.
                            artist_details = genius.artist(producer['id'])
                            # Genius geeft social media vaak terug in een lijst
                            if 'social_media' in artist_details['artist']:
                                social_media = artist_details['artist']['social_media']
                                # Zoek naar instagram in de lijst (indien aanwezig)
                                # Dit is afhankelijk van de exacte API response structuur van Genius op dat moment
                                pass 
                            
                            # Simpele fallback: Genius Profiel Link
                            ig_link = producer.get('url', '-')
                            
                        except:
                            ig_link = "Niet gevonden"

                        data_rows.append([song.title[:20], prod_name[:20], ig_link])

        # 4. Maak de Tabel
        headers = ["Song Name", "Producer", "Genius/Link"]
        table = tabulate(data_rows, headers=headers, tablefmt="simple")

        # Discord heeft een limiet van 2000 karakters. Bij lange albums moeten we splitsen.
        msg = f"**Resultaten voor {album.name}**\n```\n{table}\n```"
        
        if len(msg) > 2000:
            # Als de tabel te groot is, stuur een bestandje
            with open("results.txt", "w", encoding="utf-8") as f:
                f.write(table)
            await ctx.send("De lijst is te lang voor een bericht, hier is het bestand:", file=discord.File("results.txt"))
        else:
            await ctx.send(msg)

    except Exception as e:
        print(e)
        await ctx.send("❌ Er ging iets mis bij het ophalen van de data.")
        # Credits teruggeven bij error
        users_collection.update_one({"_id": ctx.author.id}, {"$inc": {"credits": 1}})

@bot.event
async def on_ready():
    print(f'Ingelogd als {bot.user}')

bot.run(DISCORD_TOKEN)

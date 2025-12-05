import discord
from discord.ext import commands
import lyricsgenius
import os
from pymongo import MongoClient
from datetime import datetime
from tabulate import tabulate
import asyncio
from keep_alive import keep_alive  # Importeer de wekker

# --- CONFIGURATIE (Haal deze uit Environment Variables in Render) ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GENIUS_TOKEN = os.getenv("GENIUS_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

# --- SETUP GENIUS & DISCORD ---
genius = lyricsgenius.Genius(GENIUS_TOKEN)
genius.verbose = False 
genius.remove_section_headers = True

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- SETUP DATABASE (MongoDB) ---
# Zorg dat je connectiestring correct is ingevuld bij Render Env Vars
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
    
    # Als de opgeslagen maand anders is dan de huidige maand
    if last_reset != current_month:
        return max_credits, current_month
    return user_data["credits"], last_reset

def process_credits(user_id, member):
    """Behandelt de credit logica (checken, resetten, aftrekken)."""
    max_credits = get_max_credits(member)
    
    if max_credits == 0:
        return False, "Je hebt geen 'Lite' of 'Premium' rol en kunt dit commando niet gebruiken."

    user_data = users_collection.find_one({"_id": user_id})

    if not user_data:
        # Nieuwe gebruiker aanmaken in DB
        user_data = {
            "_id": user_id, 
            "credits": max_credits, 
            "last_reset_month": datetime.now().month
        }
        users_collection.insert_one(user_data)
        current_credits = max_credits
        month_check = datetime.now().month
    else:
        # Checken of maand gereset moet worden
        current_credits, month_check = check_monthly_reset(user_data, max_credits)
        
        # Update DB als maand veranderd is
        if month_check != user_data.get("last_reset_month"):
            users_collection.update_one(
                {"_id": user_id}, 
                {"$set": {"credits": current_credits, "last_reset_month": month_check}}
            )

    if current_credits > 0:
        # Credits aftrekken
        users_collection.update_one({"_id": user_id}, {"$inc": {"credits": -1}})
        return True, current_credits - 1
    else:
        return False, "Je credits voor deze maand zijn op! Volgende maand worden ze bijgevuld."

# --- HET COMMANDO ---

@bot.command(name="album")
async def search_album(ctx, *, album_name: str):
    # 1. Check Credits
    allowed, message = process_credits(ctx.author.id, ctx.author)
    
    if not allowed:
        await ctx.send(f"❌ {message}")
        return
    
    remaining_credits = message
    status_msg = await ctx.send(f"🔍 Album **'{album_name}'** wordt gezocht... (Credits over: {remaining_credits})\n*Even geduld a.u.b., ik scan de nummers...*")

    try:
        # 2. Zoek Album via Genius (in een thread zodat de bot niet vastloopt)
        loop = asyncio.get_event_loop()
        
        # We zoeken het album. 
        # Let op: search_album kan soms het verkeerde vinden als de naam vaag is.
        album = await loop.run_in_executor(None, lambda: genius.search_album(album_name))

        if not album:
            await ctx.send(f"❌ Album '{album_name}' niet gevonden op Genius.")
            # Credits teruggeven omdat het mislukte
            users_collection.update_one({"_id": ctx.author.id}, {"$inc": {"credits": 1}})
            return

        data_rows = []
        
        # 3. Loop door de nummers
        for track in album.tracks:
            # Soms heeft het album object al producer info, maar search_song is accurater voor details
            # We gebruiken track info direct uit het album object om API calls te besparen waar mogelijk
            # Maar track.song.producer_artists is vaak de veiligste weg
            
            # Om het sneller te maken proberen we eerst de data te pakken die we al hebben
            try:
                # We halen song details op (dit kost tijd per nummer!)
                song = await loop.run_in_executor(None, lambda: genius.search_song(track.song.title, album.artist.name))
                
                if song:
                    title = song.title
                    producers = song.producer_artists
                    
                    if not producers:
                        data_rows.append([title[:25], "Onbekend", "-"])
                    else:
                        for producer in producers:
                            prod_name = producer['name']
                            # We gebruiken de url naar het Genius profiel als 'Instagram/Link'
                            # Dit is veel sneller en stabieler dan web scrapen naar IG links
                            prod_link = producer.get('url', 'Geen link')
                            
                            data_rows.append([title[:25], prod_name[:20], prod_link])
                else:
                    data_rows.append([track.song.title[:25], "-", "-"])
                    
            except Exception as e:
                print(f"Fout bij track {track.song.title}: {e}")
                continue

        # 4. Maak de Tabel
        headers = ["Song Name", "Producer", "Genius Link"]
        table = tabulate(data_rows, headers=headers, tablefmt="simple")

        # Discord limiet check (2000 karakters)
        full_message = f"**Resultaten voor {album.name} van {album.artist.name}**\n```\n{table}\n```"
        
        if len(full_message) > 2000:
            # Opslaan in bestandje en versturen
            filename = f"results_{album.name[:10].replace(' ', '_')}.txt"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(table)
            
            await ctx.send(f"De lijst voor **{album.name}** is te lang voor de chat:", file=discord.File(filename))
            os.remove(filename) # Opruimen
        else:
            await ctx.send(full_message)

    except Exception as e:
        print(f"Grote fout: {e}")
        await ctx.send("❌ Er ging iets technisch mis. Probeer het later opnieuw.")
        # Credits teruggeven bij error
        users_collection.update_one({"_id": ctx.author.id}, {"$inc": {"credits": 1}})

@bot.event
async def on_ready():
    print(f'Bot is online en ingelogd als {bot.user}')

# --- START DE BOT ---
if __name__ == "__main__":
    keep_alive() # Start de webserver voor UptimeRobot
    bot.run(DISCORD_TOKEN)

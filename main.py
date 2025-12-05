import discord
from discord.ext import commands
import lyricsgenius
import os
from pymongo import MongoClient
from datetime import datetime
from tabulate import tabulate
import asyncio
from keep_alive import keep_alive
import certifi 
import random # Om de pauzes wat menselijker te maken

# --- CONFIGURATIE ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GENIUS_TOKEN = os.getenv("GENIUS_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

# --- SETUP GENIUS (DE FIX) ---
genius = lyricsgenius.Genius(GENIUS_TOKEN)
genius.verbose = False 
genius.remove_section_headers = True
genius.retries = 3 # Probeer 3 keer als het mislukt

# HIER ZIT DE TRUC: We doen alsof we Google Chrome zijn
genius.user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- SETUP DATABASE ---
ca = certifi.where()
cluster = MongoClient(MONGO_URI, tlsCAFile=ca)
db = cluster["discord_bot_db"]
users_collection = db["users"]

# --- FUNCTIES VOOR CREDITS ---
def get_max_credits(member):
    role_names = [role.name for role in member.roles]
    if "Premium" in role_names:
        return 100
    elif "Lite" in role_names:
        return 30
    return 0 

def check_monthly_reset(user_data, max_credits):
    current_month = datetime.now().month
    last_reset = user_data.get("last_reset_month")
    if last_reset != current_month:
        return max_credits, current_month
    return user_data["credits"], last_reset

def process_credits(user_id, member):
    max_credits = get_max_credits(member)
    if max_credits == 0:
        return False, "Je hebt geen 'Lite' of 'Premium' rol."

    user_data = users_collection.find_one({"_id": user_id})

    if not user_data:
        user_data = {"_id": user_id, "credits": max_credits, "last_reset_month": datetime.now().month}
        users_collection.insert_one(user_data)
        current_credits = max_credits
        month_check = datetime.now().month
    else:
        current_credits, month_check = check_monthly_reset(user_data, max_credits)
        if month_check != user_data.get("last_reset_month"):
            users_collection.update_one({"_id": user_id}, {"$set": {"credits": current_credits, "last_reset_month": month_check}})

    if current_credits > 0:
        users_collection.update_one({"_id": user_id}, {"$inc": {"credits": -1}})
        return True, current_credits - 1
    else:
        return False, "Je credits zijn op."

# --- HET COMMANDO ---

@bot.command(name="album")
async def search_album(ctx, *, album_name: str):
    allowed, message = process_credits(ctx.author.id, ctx.author)
    
    if not allowed:
        await ctx.send(f"❌ {message}")
        return
    
    remaining_credits = message
    await ctx.send(f"🔍 Album **'{album_name}'** wordt gezocht... (Credits over: {remaining_credits})\n*Ik doe rustig aan om blokkades te voorkomen...*")

    try:
        loop = asyncio.get_event_loop()
        
        # Zoek het album
        album = await loop.run_in_executor(None, lambda: genius.search_album(album_name))

        if not album:
            await ctx.send(f"❌ Album '{album_name}' niet gevonden.")
            users_collection.update_one({"_id": ctx.author.id}, {"$inc": {"credits": 1}})
            return

        data_rows = []
        
        # Loop door de tracks
        # We beperken het aantal tracks even tot 15 om time-outs te voorkomen, 
        # of we moeten heel geduldig zijn.
        for i, track in enumerate(album.tracks):
            try:
                # FIX: Slaap 1 tot 3 seconden tussen elk nummer. 
                # Als je te snel gaat, blokkeert Cloudflare je weer.
                await asyncio.sleep(random.uniform(1.0, 2.5))
                
                # Haal song info op
                song = await loop.run_in_executor(None, lambda: genius.search_song(track.song.title, album.artist.name))
                
                if song:
                    title = song.title
                    producers = song.producer_artists
                    
                    if not producers:
                        data_rows.append([title[:25], "Onbekend", "-"])
                    else:
                        for producer in producers:
                            prod_name = producer['name']
                            prod_link = producer.get('url', 'Geen link')
                            data_rows.append([title[:25], prod_name[:20], prod_link])
                else:
                    data_rows.append([track.song.title[:25], "-", "-"])
            
            except Exception as inner_e:
                print(f"Skipped song {track.song.title} due to error: {inner_e}")
                data_rows.append([track.song.title[:25], "Error", "-"])
                continue

        # Tabel maken
        headers = ["Song", "Producer", "Link"]
        table = tabulate(data_rows, headers=headers, tablefmt="simple")

        full_message = f"**{album.name} - {album.artist.name}**\n```\n{table}\n```"
        
        if len(full_message) > 2000:
            filename = "results.txt"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(table)
            await ctx.send("Lijst is te lang:", file=discord.File(filename))
            os.remove(filename)
        else:
            await ctx.send(full_message)

    except Exception as e:
        print(f"Grote fout: {e}")
        # Check specifiek voor HTTP 403 fouten
        if "403" in str(e):
            await ctx.send("❌ Genius blokkeert de verbinding (Beveiliging). Probeer het over een uur nog eens.")
        else:
            await ctx.send("❌ Er ging iets technisch mis.")
        
        users_collection.update_one({"_id": ctx.author.id}, {"$inc": {"credits": 1}})

if __name__ == "__main__":
    keep_alive()
    bot.run(DISCORD_TOKEN)

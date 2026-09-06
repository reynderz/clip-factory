# Clip Factory — Start hier (Windows) / Start Here (Windows)

Deze gids is voor iemand die geen ervaring heeft met programmeren en dit
programma toch aan de praat moet krijgen op Windows.
This guide is for someone with zero programming experience who still needs
to get this program running on Windows.

> Let op: dit staat momenteel op de branch **`windows-setup`**. Als je de map
> al gedownload hebt en dit bestand niet ziet, vraag dan degene die je dit
> gestuurd heeft of je op de juiste branch zit, of wacht tot dit in `main`
> zit.
> Note: this currently lives on the **`windows-setup`** branch. If you
> already downloaded the folder and don't see this file, check with whoever
> sent you this that you're on the right branch, or wait until it's merged
> into `main`.

---

## Nederlands

### 1. Wat doet dit programma?

Je geeft het een Twitch VOD (een opname van een livestream). Het programma
downloadt die video, zoekt automatisch de leukste/spannendste momenten,
en jij kiest in een webpagina (in je browser) welke stukjes er echt in
moeten. Daarna maakt het programma daar automatisch korte, verticale
video's van (TikTok/Reels-formaat) met ondertitels, geluid dat gelijk
getrokken is, en optioneel de Twitch-chat erin.

### 2. Wat moet je downloaden/installeren (eenmalig)

Je hebt vijf dingen nodig. Doe dit in deze volgorde.

**a) PowerShell openen**
Druk op de Windows-toets, typ `PowerShell`, druk op Enter. Alle commando's
hieronder typ je in dit zwarte/blauwe venster, gevolgd door Enter.

**b) Git, Python en FFmpeg** — kopieer deze regel, plak hem in PowerShell
(rechtermuisknop plakt meestal), druk op Enter:

```powershell
winget install Git.Git Python.Python.3.12 Gyan.FFmpeg
```

Zeg overal "ja"/"yes" als er iets gevraagd wordt. Als het klaar is, **sluit
PowerShell volledig en open het opnieuw** (anders kent Windows de nieuwe
programma's nog niet).

**c) TwitchDownloaderCLI** (nodig om de chat van de stream te downloaden)
1. Ga naar: https://github.com/lay295/TwitchDownloader/releases
2. Download de nieuwste `TwitchDownloaderCLI...win-x64.zip`
3. Pak de zip uit naar bijvoorbeeld `C:\Programs\TwitchDownloaderCLI\`
4. Zet die map op je PATH: Windows-toets → typ "omgevingsvariabelen" →
   open "Omgevingsvariabelen bewerken" → onder "Gebruikersvariabelen"
   dubbelklik op `Path` → "Nieuw" → plak het mappad → OK → OK → OK.
5. Sluit PowerShell opnieuw en open het weer.

**d) Het lettertype "Komika Axis"** (voor de ondertitels)
1. Je krijgt/vindt een bestand `KOMIKAX_.ttf` (meegeleverd door wie je dit
   project gaf, of zoek "Komika Axis font download").
2. Rechtermuisklik op het bestand → **"Installeren voor alle gebruikers"**
   (of gewoon "Installeren" als dat de enige optie is).

**e) Controleren of alles gelukt is** — typ dit in PowerShell:

```powershell
git --version
python --version
ffmpeg -version
TwitchDownloaderCLI --version
```

Als bij elk commando een versienummer verschijnt (geen rode foutmelding),
ben je klaar voor de volgende stap.

### 3. Het programma downloaden

Kies één optie.

**Optie A — makkelijkst, geen Git-kennis nodig:**
Ga naar de GitHub-pagina van het project in je browser, klik op de groene
knop **"Code"** → **"Download ZIP"**. Pak de zip uit, bijvoorbeeld naar
`C:\Users\JouwNaam\clip-factory`.

**Optie B — met Git (handiger als er later updates komen):**
```powershell
cd $HOME
git clone https://github.com/MirtheReynders/clip-factory.git
cd clip-factory
git checkout windows-setup
```

### 4. Eenmalig instellen

In PowerShell, in de `clip-factory`-map (`cd C:\Users\JouwNaam\clip-factory`
als je daar nog niet bent):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Krijg je een rode foutmelding over "execution policies" bij
`Activate.ps1`? Typ dan dit één keer, kies "ja"/"Y", en probeer de
`Activate.ps1`-regel opnieuw:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Je moet die `.venv\Scripts\Activate.ps1`-regel voortaan **elke keer**
opnieuw typen als je een nieuw PowerShell-venster opent om dit programma te
gebruiken (je ziet dan `(.venv)` voor je regel verschijnen — dat betekent
dat het gelukt is).

### 5. Het programma gebruiken

**Stap 1 — zoek het VOD-nummer.** Ga naar de Twitch-video die je wilt
gebruiken, kijk naar de link in je browser, bijvoorbeeld:
`https://www.twitch.tv/videos/2866107546` → het nummer `2866107546` is het
VOD-id.

**Stap 2 — start het programma** (in de `clip-factory`-map, met `(.venv)`
actief):

```powershell
python gui.py 2866107546
```

Vervang `2866107546` door jouw eigen VOD-nummer. Er opent automatisch een
tabblad in je browser (`http://localhost:5002`).

**Stap 3 — in de browser:**
1. Klik op **"Detect"** (of vergelijkbare knop) — dit downloadt de video +
   chat en zoekt automatisch de beste momenten. Dit kan **15–25 minuten**
   duren de eerste keer, dat is normaal.
2. Blader door de gevonden momenten, keur de leuke stukjes goed, kies
   eventueel welke chatberichten getoond worden.
3. Klik op **"Render"** — dit maakt de uiteindelijke video's. Dit kan ook
   een tijd duren, afhankelijk van hoeveel clips je goedkeurde.
4. Klaar! De filmpjes staan in de map `out\<VOD-nummer>\` binnen
   `clip-factory`.

Om het venster van het programma te stoppen: klik in het PowerShell-venster
en druk `Ctrl+C`.

### 6. Veelgestelde problemen

- **"python is not recognized" / "git is not recognized"** → PowerShell
  niet opnieuw geopend na de installatie. Sluit alle PowerShell-vensters en
  open een nieuwe.
- **Ondertitels zien er raar/standaard uit** → het lettertype is niet goed
  geïnstalleerd, zie stap 2d hierboven.
- **"TwitchDownloaderCLI is not recognized"** → de map staat niet op je
  PATH, zie stap 2c hierboven, en open PowerShell opnieuw.
- **Activate.ps1 geeft een rode foutmelding** → zie de
  `Set-ExecutionPolicy`-regel in stap 4.
- **Alles gaat heel langzaam** → normaal bij de eerste "Detect" (transcriptie
  kost tijd), en bij "Render" met veel goedgekeurde clips. Een snellere
  videokaart (NVIDIA/AMD/Intel) kan helpen — vraag daarvoor iemand met meer
  ervaring om `configs\_global.yaml` → `output.encoder` aan te passen.

---

## English

### 1. What does this program do?

You give it a Twitch VOD (a saved livestream recording). The program
downloads that video, automatically finds the best/most exciting moments,
and you pick which clips to keep in a webpage (in your browser). It then
automatically turns those into short, vertical videos (TikTok/Reels
format) with subtitles, normalized audio, and optionally the Twitch chat
overlaid.

### 2. What to download/install (one time only)

You need five things. Do this in order.

**a) Open PowerShell**
Press the Windows key, type `PowerShell`, press Enter. Every command below
gets typed into this window, followed by Enter.

**b) Git, Python, and FFmpeg** — copy this line, paste it into PowerShell
(right-click usually pastes), press Enter:

```powershell
winget install Git.Git Python.Python.3.12 Gyan.FFmpeg
```

Say "yes" to any prompts. Once it's done, **close PowerShell completely and
reopen it** (otherwise Windows won't recognize the new programs yet).

**c) TwitchDownloaderCLI** (needed to download the stream's chat)
1. Go to: https://github.com/lay295/TwitchDownloader/releases
2. Download the newest `TwitchDownloaderCLI...win-x64.zip`
3. Extract the zip somewhere, e.g. `C:\Programs\TwitchDownloaderCLI\`
4. Add that folder to your PATH: press Windows key → type "environment
   variables" → open "Edit environment variables for your account" →
   under "User variables" double-click `Path` → "New" → paste the folder
   path → OK → OK → OK.
5. Close PowerShell and reopen it again.

**d) The "Komika Axis" font** (used for the subtitles)
1. You'll receive/find a file called `KOMIKAX_.ttf` (from whoever gave you
   this project, or search "Komika Axis font download").
2. Right-click the file → **"Install for all users"** (or just "Install"
   if that's the only option).

**e) Check everything worked** — type this into PowerShell:

```powershell
git --version
python --version
ffmpeg -version
TwitchDownloaderCLI --version
```

If each command prints a version number (no red error text), you're ready
for the next step.

### 3. Downloading the program

Pick one option.

**Option A — easiest, no Git knowledge needed:**
Go to the project's GitHub page in your browser, click the green
**"Code"** button → **"Download ZIP"**. Extract the zip, for example to
`C:\Users\YourName\clip-factory`.

**Option B — with Git (better if updates come later):**
```powershell
cd $HOME
git clone https://github.com/MirtheReynders/clip-factory.git
cd clip-factory
git checkout windows-setup
```

### 4. One-time setup

In PowerShell, inside the `clip-factory` folder (`cd
C:\Users\YourName\clip-factory` if you're not already there):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Getting a red "execution policies" error on `Activate.ps1`? Run this once,
answer "yes"/"Y", then try the `Activate.ps1` line again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

You'll need to run that `.venv\Scripts\Activate.ps1` line again **every
time** you open a new PowerShell window to use this program (you'll see
`(.venv)` appear before your prompt when it worked).

### 5. Using the program

**Step 1 — find the VOD number.** Go to the Twitch video you want to use
and look at the link in your browser, for example:
`https://www.twitch.tv/videos/2866107546` → the number `2866107546` is the
VOD id.

**Step 2 — start the program** (inside the `clip-factory` folder, with
`(.venv)` active):

```powershell
python gui.py 2866107546
```

Replace `2866107546` with your own VOD number. A browser tab opens
automatically (`http://localhost:5002`).

**Step 3 — in the browser:**
1. Click **"Detect"** (or the similarly-named button) — this downloads the
   video + chat and automatically finds the best moments. This can take
   **15–25 minutes** the first time, that's normal.
2. Browse through the found moments, approve the good clips, optionally
   pick which chat messages get shown.
3. Click **"Render"** — this creates the final videos. This also takes a
   while, depending on how many clips you approved.
4. Done! The videos are in the `out\<VOD-number>\` folder inside
   `clip-factory`.

To stop the program's window: click into the PowerShell window and press
`Ctrl+C`.

### 6. Common problems

- **"python is not recognized" / "git is not recognized"** → PowerShell
  wasn't reopened after installing. Close all PowerShell windows and open
  a new one.
- **Subtitles look wrong/default** → the font wasn't installed properly,
  see step 2d above.
- **"TwitchDownloaderCLI is not recognized"** → its folder isn't on your
  PATH, see step 2c above, then reopen PowerShell.
- **Activate.ps1 shows a red error** → see the `Set-ExecutionPolicy` line
  in step 4.
- **Everything is very slow** → normal for the first "Detect" run
  (transcription takes time) and for "Render" with many approved clips. A
  faster GPU (NVIDIA/AMD/Intel) can help — ask someone more experienced to
  adjust `configs\_global.yaml` → `output.encoder`.

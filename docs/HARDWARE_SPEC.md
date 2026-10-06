# Orb Appliance: Hardware Specification

*Version 3 · first written 1 August 2026, measurements re-checked 3 September
2026, hardware and Apple India pricing re-checked against apple.com on
7 September 2026 for the M5 generation · For the hospital's CIO and IT lead.
This is the document a purchase order is raised from.*

**What changed in version 3.** Only the hardware. Every measurement in section 2
is unchanged and still comes from the M2 Max development machine. Apple has
moved the Mac Studio to M5 Max / M5 Ultra, so the parts, the prices, the UPS
rating and the bandwidth projections have all been restated; the reasoning
behind each number is the same reasoning as version 2. A companion document,
[CAPACITY_200_BED.md](CAPACITY_200_BED.md), covers a whole 200-bed hospital
rather than the 60-bed pilot ward this one specifies.

Orb runs on one computer that the hospital owns and keeps in its own building. I
call that computer the appliance. Everything the system does (the patient
record, the monitoring, the AI) happens on it. Nothing goes to a cloud service,
which is why the machine has to be specified properly rather than picked from
whatever is spare.

This document says exactly what to buy, why each number is what it is, what the
hospital has to provide alongside it, what it costs, and what breaks if the
specification is cut. Every performance figure quoted here was measured on my
development machine and is labelled as such; where I have not measured
something, section 11 says so rather than leaving you to find out.

**Timing, and it is tighter than it was.** Apple build-to-order configurations
are not stocked locally and typically take two to four weeks to arrive. As of
7 September 2026 the M5 Mac Studio is **pre-order, with availability from
22 September**, so a launch-window build-to-order will be at the back of a queue
and the usual two-to-four weeks should be read as a floor, not an estimate. The
deployment plan runs thirteen weeks from signature to shadow go-live, and the
order has to be placed by the end of week 3. If it has not been placed by then,
I buy the machines and invoice them under the licence-plus-hardware terms,
because the schedule cannot absorb the delay.

---

## 1. What the appliance actually has to do

At any moment during the pilot the machine is doing all of these at once:

| Job | What it means for the hardware |
|---|---|
| Hold the clinical record | An encrypted SQLite database, read and written constantly. Disk and memory. |
| Ingest vital signs from up to 60 beds | Small, continuous writes. Never allowed to fall behind. |
| Score every patient with NEWS2 as data arrives | CPU, continuous. |
| Serve the web application to every ward device | Modest. The server process peaked at 373 MB of memory in the valid 15-minute soak at a 60-bed census. |
| Run the large clinical model (MedGemma 27B) for Sage | 17 GB resident, and the single biggest determinant of how fast Sage feels. |
| Run the small ambient model (MedGemma 4B) continuously | 3.3 GB resident, and busy: measured at a 76% GPU duty cycle during an alert storm before I bounded background work. |
| Run speech-to-text (Whisper large-v3) for dictation | Loads on demand. Budget about 3 GB. |
| Run search embeddings and reranking (BGE-M3 and reranker) | About 2.3 GB and 1.1 GB when loaded, in a separate process. |
| Take and verify a backup every night | Disk throughput and free space. |

The crucial point for sizing: these models must all stay in memory at the same
time. If they do not, the operating system evicts one to load another, and every
eviction is paid for by a clinician waiting at a screen. I have measured that
cost directly: the first prefill of a 1,067-token prompt on a cold cache took
6.9 seconds; with the cache warm the same work took 0.4 seconds. That is a
seventeen-fold difference, and it is what insufficient memory buys you: not a
crash, just a system that is slow at exactly the wrong moments.

---

## 2. The measurements this specification is built on

All figures below were taken on my development machine, an Apple M2 Max with
64 GB of unified memory. They are real measurements from this repository, not
vendor claims or estimates.

### Model memory and disk footprints

| Component | Size | How I know |
|---|---|---|
| MedGemma 27B (Sage's clinical model) | 17 GB | `ollama list` on the development machine |
| MedGemma 4B (ambient model) | 3.3 GB | same |
| BGE-M3 embeddings | 4.3 GB on disk, about 2.3 GB loaded at fp16 | HuggingFace cache measured; fp16 load path in `services/clinical_knowledge.py` |
| BGE reranker v2-m3 | 2.1 GB on disk | HuggingFace cache measured; used only when reranking is explicitly requested |
| Whisper large-v3 (dictation) | about 3 GB | Published size of the MLX build; not yet measured on my hardware, see section 11 |
| Total model working set | about 30 GB | Sum of the above |

A 815 MB Gemma 3 1B model is also pulled at setup. No code path uses it today; I
list it so the disk total reconciles.

### Application load at pilot volume

The valid soak is from 11 August 2026: 15 minutes at the pilot's census (60
patients, 5 at start plus 55 admitted during the run), run twice with the same
result. Full reports are in `docs/perf/soak-20260811-050842` and `-052437`.

| Endpoint | Requests | Errors | Median | 99th percentile |
|---|---|---|---|---|
| Health probe | 1,795 | 0 | 3.1 ms | 5.3 ms |
| Patient board | 900 | 0 | 7.2 ms | 35.9 ms |
| Patient chart | 300 | 0 | 19.8 ms | 75.4 ms |
| Messaging read | 225 | 0 | 27.8 ms | 103.3 ms |
| Forecast | 60 | 0 | 4.0 ms | 110.4 ms |

Zero errors across 3,634 requests; no request over 200 ms; server memory 285 to
373 MB; boot to healthy 2.1 seconds. Every service-level objective passed.

I voided the soak reports taken before 11 August, including the one the first
version of this document quoted, because the harness had been measuring the
reloader parent process under the development profile. The numbers above are
from the real worker under the right profile; the memory figure is higher than
the voided one for exactly that reason.

Read that table the right way. It says the application is comfortable at pilot
volume: the ward-facing screens stay responsive. It says nothing about AI speed,
which is the part that is bandwidth-hungry and is dealt with next.

### AI speed, and why memory bandwidth is the number that matters

Measured directly against the 27B model on the M2 Max:

- Prefill (reading the question and its evidence): about 155 tokens per second
- Decode (writing the answer): about 15 tokens per second

Decode speed on Apple Silicon is limited almost entirely by memory bandwidth,
not by processor cores. The M2 Max I measured on has 400 GB/s. This is the one
place where paying more genuinely buys the pilot something the pilot is judged
on: Sage's first-words latency is a stated quality bar, met for simple questions
(3.2 to 4.2 s) and missed for questions that pull guideline text (8 to 12 s
after the August work). On 14 August the alert-to-screen path measured 59 ms;
the language model's written assessment of the same alert took 49 s, which is
why the tier that pages a nurse has no model in it.

### Database size and growth

| Observation | Value |
|---|---|
| Live encrypted database, 1 August 2026 | 293 MiB (about 307 MB) |
| Live encrypted database, 3 September 2026 | 647 MiB (about 678 MB), after five more weeks of simulator load |
| Verified backup, 30 July 2026 | 157 MiB (about 165 MB) |
| Growth measured over a 50-hour window in August | about 2.8 MB per hour, about 68 MB per day |
| Vital-sign readings held on 1 August | 62,492 across 30 patients |
| Guideline corpus (4 source documents, 19 indexed chunks) | 4.3 MB |

Sizes are quoted both ways because the tools disagree: `du -h` and `ls -lh`
report MiB, a purchase order talks in MB. They are the same file.

This growth figure is an upper bound, and an honest one has to say why. It comes
from a development machine running the synthetic-data simulator, which writes
roughly one vitals reading per patient every two minutes and also runs report
and lab generators. A real ward using manual nurse entry will write far less; a
ward with full monitor integration will write at a broadly comparable rate. The
real number gets measured in the first week of shadow mode. I have sized storage
so that being wrong about it by a factor of three does not matter.

---

## 3. The primary appliance

### Buy this

| Item | Specification |
|---|---|
| Model | Apple Mac Studio |
| Chip | M5 Ultra (30-core CPU, 64-core GPU) |
| Unified memory | 96 GB |
| Storage | 1 TB SSD |
| Power supply | Standard, 220 to 240 V |
| Warranty | AppleCare+ for Mac Studio, 3 years, with the service caveat in section 8.3 |

If a newer Mac Studio generation is current when you order, do not copy the
part number; apply the rule instead: unified memory of 96 GB or more, and the
highest memory bandwidth available within budget. Those two properties are what
this specification is actually asking for.

### Why each number

**Why a Mac Studio and not a server.** Every AI model Orb runs is compiled for
Apple Silicon: Whisper runs through Apple's MLX framework, the embedding models
run on Metal, and the language models run through Ollama's Metal backend. Apple
Silicon's unified memory means the GPU can address the whole 96 GB without a
separate graphics card, which is how a 17 GB model and a 3.3 GB model coexist on
one machine at all. An equivalent x86 server would need two datacentre GPUs, a
rack, and cooling the ward does not have.

**Why 96 GB and not 64 GB.** The measured model working set is about 30 GB. My
64 GB development machine runs it, so 64 GB is a proven floor, not a guess. I
specify 96 GB anyway, for three reasons I can name:

1. macOS reserves a share of unified memory and will not let the GPU take it
   all. At 64 GB the headroom above a 30 GB model set plus the operating system
   plus the application is real but thin, and it is consumed by the very thing
   that makes Sage feel fast: cached conversation state for concurrent
   clinicians.
2. The deployment is thirteen weeks against a date. Discovering mid-pilot that
   the appliance needs more memory is not a purchase decision, it is a schedule
   failure.
3. The gap between 64 GB and 96 GB is roughly one and a half lakh rupees. The
   gap between a working pilot and a stalled one is the pilot.

**Why the M5 Ultra and not the M5 Max.** This is the one genuinely arguable line
in this document, so here is the argument in full. The M5 Max costs less than
half as much and is a fast processor for most work. The M5 Ultra's advantage is
memory bandwidth: Apple publishes **1.2 TB/s** for the M5 Ultra against
460 GB/s for the M5 Max (614 GB/s in its 40-core GPU configuration) and
400 GB/s for the M2 Max I measured on. Because Sage's answer speed is
bandwidth-bound at decode, and because "how long a clinician waits for Sage" is
one of the pilot's stated quality bars, bandwidth is the lever that moves the
number the pilot will be judged on.

I have not measured this. Scaling my measured 15 tokens per second by the
bandwidth ratio suggests roughly 45 tokens per second on an M5 Ultra, which
would cut Sage's answer time to about a third. That is an expectation derived
from a published specification, not a measurement, and it now spans three chip
generations rather than one, so treat it as a hypothesis to test rather than a
promise. The first thing I will do on the delivered appliance is measure it and
publish the real figure. If the hospital would rather not pay for an
expectation, an M5 Max with 128 GB is a defensible alternative that costs less
and gives more memory and less bandwidth; say so and I will specify it that way.

**One thing the M5 generation changes for the better.** The M5 Ultra's base
configuration is 96 GB of unified memory and a 1 TB SSD, which is exactly what
this document asks for. On the previous generation both were paid upgrades. The
memory argument below is now an argument for the base machine, not for an
options list.

**And one thing it opens up.** The M5 Ultra can be configured to 256 GB. At
96 GB that would be money spent on memory the 30 GB model set cannot use. At
256 GB it buys something specific: several 27B instances resident at once,
which is what serving more than one clinician's Sage turn concurrently
requires. That is not a pilot-scale need and I am not specifying it here, but a
hospital planning past one ward should read `CAPACITY_200_BED.md` section 6
before ordering 96 GB.

**Why 1 TB and not 512 GB.** Adding up what has to fit:

| What | Space |
|---|---|
| macOS and system | about 30 GB |
| AI model weights (Ollama 21 GB, HuggingFace 6.4 GB, Whisper about 3 GB) | about 30 GB |
| Clinical database at the end of a 13-week pilot, at measured growth | about 6 GB |
| 14 retained daily backups (full copies, not increments) | about 85 GB |
| Free space required to perform a restore (room for both copies) | 2x the database |
| Uploaded media, dictation audio and imaging | Unmeasured; see section 11 |
| Pilot-year total | about 200 GB |
| Three-year total at measured growth | about 450 to 500 GB |

512 GB would survive the pilot and then become an annual pruning chore, and the
moment it matters is a restore, the one time you least want to be deleting files
to make room. 1 TB removes the problem for the life of the machine.

---

## 4. The standby appliance

### Buy this

| Item | Specification |
|---|---|
| Model | Apple Mac Studio |
| Chip | M5 Max (18-core CPU, 40-core GPU) |
| Unified memory | 64 GB |
| Storage | 1 TB SSD |

### Why it exists, and why it is not identical

The standby is a second machine that holds a nightly restored copy of the
database and can take over the ward if the primary dies. It is insurance,
designed never to be used. The reliability plan is that the appliance does not
fail: soak tests, load tests at pilot volume and health trending are what that
is built from. The standby is what makes it safe to be wrong about that.

It is specified smaller than the primary because 64 GB is the measured floor for
the full model set: my own development machine is a 64 GB Apple Silicon machine
and it runs everything in this document. In a failover the ward keeps its
complete record, its monitoring, its ordering and its messaging at full speed.
Sage answers more slowly. That is a trade I am stating openly rather than
discovering during an incident.

If the hospital prefers two identical machines (one image, one runbook, one set
of measurements, interchangeable parts), that is a better engineering answer and
costs roughly one and a half lakh more. It is a reasonable place to spend money.

The standby is only real if it is exercised. The monthly restore drill runs on
the standby, not on the primary. A standby that has never had a backup restored
onto it is a machine, not a plan. I have not yet bought a second machine myself,
so as of today the standby procedure has been rehearsed in software and not on
two physical Macs; that rehearsal is part of the install.

---

## 5. Backup media: the rotating encrypted SSDs

### Buy this

| Item | Quantity | Specification |
|---|---|---|
| Rugged external SSD | 3 | 2 TB, USB-C (USB 3.2 Gen 2 or better), shock-rated |

### How they are used

Backups run automatically every night at 02:30 once the schedule is installed
on the appliance. Each backup is verified before anything is rotated out; a
failed verification means nothing is deleted, by design. Every verified backup
is copied to whichever external SSD is currently attached.

The three drives rotate weekly:

| Drive | Where it lives |
|---|---|
| A | Attached to the appliance, receiving nightly copies |
| B | In the hospital safe, in a sealed container, in a different room from the appliance |
| C | Off site, held by me |

Swap A and B every week. Rotate C monthly. Each drive is formatted as an
encrypted APFS volume; the backup files are already SQLCipher-encrypted, so this
is a second layer, not the only one, and it means a drive lost in transit is a
nuisance rather than an incident.

The key envelope and the ciphertext never travel in the same container. A backup
drive and the key to read it, in one bag, is not a backup strategy.

### Why 2 TB and why three

2 TB holds a year of retained daily backups with room to spare, so the drive
does not silently stop accepting copies; a full off-box volume is one of the
quieter ways to lose a backup regime. Three drives is the smallest number that
survives the three failures that actually happen: a drive fails, the building
has a fire, and someone forgets to bring the drive back.

---

## 6. Power and the UPS

### Buy this

| Item | Specification |
|---|---|
| UPS | 1500 VA / 1000 W minimum, line-interactive, pure sine wave output |
| Signalling | USB, enumerating as a standard USB HID Power Device so macOS recognises it |
| Runtime target | 20 minutes or more at 250 W load |
| Outlets | Enough for the appliance, the network switch and the console display |

### Why this is not optional

Apple rates the M5 Mac Studio at **480 W maximum continuous**. Sustained AI
inference will not sit at that ceiling, and I expect the real draw to be in the
region of 250 to 350 W, but the UPS has to be sized against the rating rather
than my expectation, and the appliance is not the only thing on it. A 1500 VA /
1000 W unit carries the appliance, the standby, the switch and the console with
enough margin to ride out the great majority of short interruptions outright.

The 1000 VA / 600 W unit specified in the first version of this document was
sized against the previous generation's draw. It is no longer enough, and this
is exactly the kind of number that gets copied forward from an old
specification without being re-checked.

But the real reason the UPS matters is a conflict between two things that are
both correct on their own, and the hospital should understand it before
purchase:

- The security posture wants FileVault on. Full-disk encryption is the right
  answer for a machine holding a hospital's clinical record in a room people
  walk past.
- The backup schedule requires automatic login. Orb's nightly backup runs as a
  user LaunchAgent rather than a root daemon, deliberately, because making it
  root would mean root reading the operator's encryption key, concentrating the
  key and the ciphertext at one privilege level, which is the exact thing
  encryption at rest exists to prevent. A LaunchAgent only runs while the
  operator is logged in, so the appliance must log in automatically after a
  reboot.

With FileVault enabled, an unattended reboot stops at the unlock screen. It does
not reach the desktop; Orb does not start, backups do not run, and vitals
ingestion does not resume until a person physically types the password. Orb is
operated on-site only, with no inbound remote access, and I am under an hour
away. So every power cut long enough to drop the machine costs the ward at least
an hour without Orb, and there is nothing clever I can do about it from a
distance.

The UPS is what stops that from happening. In an Indian hospital with a
generator changeover gap, that is not a hypothetical.

I will put this decision in front of the CIO explicitly at install, with both
options written down: FileVault on and accept manual unlock after a long outage,
or FileVault off and rely on the database and media encryption Orb already
applies plus physical security. The database is SQLCipher-encrypted either way.
I recommend FileVault on, plus the UPS, plus generator-backed power for the
socket.

### What the hospital must provide

- A generator-backed, essential-services circuit for the UPS input, the same
  class of supply that ward equipment runs on.
- Two spare outlets at the appliance location.

---

## 7. Network

### Buy this

| Item | Specification |
|---|---|
| Switch | 8-port managed gigabit switch, if the location does not already have a spare managed port |
| Cabling | Cat 6, appliance to switch; a second run for the standby |

### What the hospital must provide

| Requirement | Why |
|---|---|
| Wired gigabit ethernet to the appliance and to the standby | Not Wi-Fi. A dropped ingestion packet becomes a gap in the vital-sign record, and a gap in the record becomes a hole in the pilot's denominators that cannot be filled afterwards. |
| A fixed IP address (static, or a DHCP reservation) | Every ward device, and every monitor integration, addresses the appliance by that address. It cannot change. |
| Reachability from ward devices (nurse-station desktops, bedside tablets, clinicians' phones) on the ward Wi-Fi | This is the only way anybody uses the system. |
| Reachability from the patient monitors in scope for integration | Monitor integration is read-only: Orb reads vital signs and can never send anything to a monitor or change a setting on one. The adapter has been tested against a simulator and is first run against your real devices with your biomedical team present. |
| No inbound access from the internet. None. | There is no remote support path and I am not asking for one. |
| No outbound internet either | Orb is strictly offline and makes no internet connection of any kind. The outbound health heartbeat was removed on 4 October 2026 (monitoring is a daily on-site check), and on 6 October the Pulse panel's public feeds (weather, air quality, influenza surveillance, drug recalls) were replaced by a seasonal calendar that ships with Orb. Block all outbound traffic from the appliance at the firewall. |

A dedicated VLAN for the appliance and the monitors is good practice and I will
work with whatever the hospital's network team prefers.

---

## 8. Physical siting and security

### 8.1 Where it goes

| Requirement | Detail |
|---|---|
| A locked room or a locked cabinet | The hospital's existing server or IT room is ideal. If it must live on the ward, it needs a lockable cabinet with ventilation. |
| Named access list | Write down who can physically reach the machine. Keep it short. |
| Ventilation | The Mac Studio is quiet but it is doing sustained GPU work. Do not put it in a sealed cupboard. |
| Not in a patient area | It is not a clinical device and nothing at the bedside depends on it. |
| Physical tether | Kensington-style lock to the cabinet or bench for both machines. |

### 8.2 What else lives in the safe

- Two sealed envelopes, each containing both encryption keys: the database key
  and the uploads key. One in the hospital safe, one held off site by me. Losing
  both means every backup is permanently unreadable ciphertext; there is no
  vendor override and no recovery. This is set up in a witnessed key ceremony
  before real patient data arrives.
- Backup drive B, in a separate sealed container from the key envelope.

### 8.3 The service caveat: read before buying AppleCare

The Mac Studio's storage is not removable. If the machine is sent for warranty
service, its disk goes with it. The database on that disk is SQLCipher-encrypted
and, with FileVault on, the volume is encrypted too, so what leaves the building
is ciphertext. That is a good position but it is not a policy.

The policy is: before any service dispatch, the machine is erased (Erase All
Content and Settings) and the ward is moved to the standby. The machine is
rebuilt from the last verified backup and the escrowed key when it returns. This
takes an afternoon and it must be rehearsed once before it is needed.

AppleCare+ is still worth buying; a three-year single-appliance deployment with
no spare in the country is exactly the case it exists for.

---

## 9. Client devices: hospital-provided

Orb is a web application. It runs in a browser and there is nothing to install
on any ward device.

| Device | Minimum | Notes |
|---|---|---|
| Nurse-station desktop | Any machine from the last five years or so, running a current Chrome, Edge or Safari; 1366x768 or better | The board and chart are designed for 1440x900 and up |
| Bedside tablet | 10-inch or larger, current browser | Touch targets are sized for gloved use; the tablet vitals-entry screen is built for this |
| Clinician phone | Any current smartphone browser | For Relay messaging and alerts |
| Console display for the appliance | One 24-inch display, keyboard and mouse, shared between the two Macs via a 2-port USB-C KVM | The appliance is not used interactively day to day, but the operator needs a console |

The Orb interface is English. A Hindi toggle exists and today it translates the
navigation only (about 65 strings); everything else falls back to English. Do
not plan around a fully Hindi interface for this pilot. There is a Night Shift
high-contrast, low-brightness theme for overnight wards.

---

## 10. Cost

### Configuration and indicative price

These figures are indicative and are not a quotation. Row 1 is Apple India's own
listed price read on 7 September 2026, and it needs no upgrade: 96 GB and 1 TB
is the M5 Ultra's base configuration. Row 2 is an ESTIMATE. The M5 Max starts at
₹2,79,900 with 36 GB and 512 GB, and the standby needs 64 GB, 1 TB and the
40-core GPU on top of that; Apple's configurator does not publish the upgrade
deltas, so the range in that row is my expectation and is the single least
reliable number in this table. Everything else is the order of magnitude I
expect from Apple India list pricing as I understood it on 1 August
2026, inclusive of GST. Raise the purchase order against a formal quote from
your Apple Authorised Reseller. The configuration column is what must be matched
exactly; the price column is what your finance team will replace with the real
number.

| # | Item | Configuration | Qty | Indicative (₹) |
|---|---|---|---|---|
| 1 | Mac Studio, primary appliance | M5 Ultra 30C/64C, 96 GB, 1 TB | 1 | 6,29,900 |
| 2 | Mac Studio, warm standby | M5 Max 18C/40C, 64 GB, 1 TB | 1 | 3,40,000 to 3,90,000 |
| 3 | AppleCare+ for Mac Studio, 3 years | n/a | 2 | 40,000 to 55,000 |
| 4 | Rugged external SSD | 2 TB USB-C | 3 | 45,000 to 72,000 |
| 5 | UPS | 1500 VA / 1000 W, pure sine, USB HID | 1 | 18,000 to 28,000 |
| 6 | Managed gigabit switch | 8-port | 1 | 8,000 to 15,000 |
| 7 | Console display, keyboard, mouse, USB-C KVM | 24-inch | 1 set | 25,000 to 40,000 |
| 8 | Lockable cabinet or rack shelf, physical locks | n/a | 1 | 10,000 to 25,000 |
| 9 | Cat 6 cabling, mounting, labelling | n/a | n/a | 5,000 to 10,000 |
| | Total | | | ₹11,20,900 to 12,64,900 |

Not included, because the hospital already has them or buys them separately:
ward desktops, bedside tablets, clinician phones, the safe, and electrical work
for the essential-services circuit.

### If the budget will not stretch

In the order I would give things up:

1. AppleCare+ on the standby (saves ₹20,000 to 27,000). The standby is the
   spare; a spare for the spare is a luxury.
2. Two backup drives instead of three (saves ₹15,000 to 24,000). You lose the
   monthly off-site rotation and keep the weekly swap. Acceptable for a pilot,
   not for a live hospital.
3. Primary as M5 Max 128 GB instead of M5 Ultra 96 GB (saves roughly ₹2,00,000
   to ₹2,50,000). More memory, less than half the bandwidth, slower Sage
   answers. This is a real trade and the hospital is entitled to make it.

What I would not give up, in any budget: the standby machine, the UPS, the wired
network, or the 1 TB storage. Each of those protects the clinical record rather
than the experience.

---

## 11. What I have not measured

Stated so nobody discovers it later.

- I have not run Orb on a Mac Studio. Every figure in section 2 comes from an
  M2 Max laptop with 64 GB. The application numbers will transfer or improve.
  The AI speed projection for the M5 Ultra is derived from Apple's published
  memory bandwidth, not from my own measurement, and is now extrapolated three
  chip generations from the machine I measured on. First task on the delivered
  appliance: re-run the soak and the model benchmarks and publish the real
  numbers against this document.
- Whisper large-v3's resident memory is not measured on my hardware, and the
  speech runtime is not installed on my development machine today; it is
  installed on the appliance. The 3 GB figure is the published size of the MLX
  build. It is a small term in a 30 GB working set, but it is not mine.
- Uploaded media growth is unmeasured. Dictation audio and imaging are stored
  encrypted on the appliance and backed up with the database. I have no
  ward-realistic figure for how much dictation a real ward produces. Storage is
  sized with enough slack to absorb being wrong; I will measure it in shadow
  week 1.
- Database growth is measured under synthetic load, not real ward load. See
  section 2. The real figure comes from the first shadow week.
- The bedside-monitor adapter has only run against a simulator. It is first run
  against real devices on your ward, with your biomedical team, during the
  integration phase.
- The appliance froze under load three times in the week of 28 July 2026, and a
  dry run on 14 August found six shutdown hangs. All were root-caused and fixed:
  a network-proxy lookup running on the main event loop, an encryption-driver
  connection close holding the Python interpreter lock, an in-process embedding
  model starving the same lock, and six places where a cancelled task was
  swallowed. They are named here because a vendor that hides its incidents is
  not one you should trust with a ward. Each fix is covered by a regression test,
  and the health probe under alert-storm conditions is re-run before every phase
  gate.

### Software prerequisites (for the IT team's records)

The appliance runs macOS on Apple Silicon. Orb needs Python 3.11 or newer,
Node.js 20 or newer to build the interface, and Ollama to host the language
models. My installer sets all of it up in one command, and the hospital does not
need to provide any of it.

One version pin matters and it is worth writing on the machine. Database
encryption depends on a SQLCipher driver, and the distribution I pin ships a
prebuilt package for CPython 3.12 on Apple Silicon and no other combination.
Install Python 3.12. This is called out because of how it fails: if the driver
cannot be imported, Orb does not refuse to start; it logs a warning and runs on
an unencrypted database. Somebody re-imaging the machine with a different Python
would get a working system holding the hospital's record in plain text, and
nothing on screen would say so. Check the startup log after any rebuild rather
than assuming.

---

## 12. What happens if it is under-specified

Concrete, in the order I expect them to bite.

| Cut | What actually happens |
|---|---|
| 36 or 48 GB of memory | The 27B model, the 4B ambient model, Whisper and the embedding models cannot all stay resident. macOS evicts one to load another, on every switch. I have measured that cost: 6.9 seconds versus 0.4 seconds for the same prefill. Sage becomes slow exactly when the ward is busy, because that is when the ambient model is working hardest. Nothing crashes. It just stops being worth using. |
| A Mac mini instead of a Mac Studio | The M5 Pro mini tops out at 64 GB, which is the floor, and Apple publishes 307 GB/s of memory bandwidth against the 400 GB/s I measured on. Since Sage's answer speed is bandwidth-bound, expect answers roughly a quarter slower than my already imperfect measurements, with no upgrade path. |
| 512 GB storage | Survives the pilot, then becomes an annual chore. The failure lands during a restore, when you need free space equal to the database and are deleting files under pressure at the worst possible moment. |
| No standby machine | A dead appliance means the ward is on paper downtime packs until a replacement is bought, configured and restored: days, not hours, because the machine has to be ordered. |
| No UPS | Every power interruption stops the machine at the FileVault unlock screen (section 6). Orb is down, ingestion is down, and backups are down until someone drives to the hospital. In the shadow pilot this also punches an unfillable hole in the evidence, because a gap in the vitals record cannot be reconstructed. |
| Wi-Fi instead of wired | Intermittent ingestion gaps. They will not be obvious; they surface weeks later as missing denominators in the pilot scorecard, and by then the data is gone. |
| Fewer than two backup drives | On-appliance backups protect you against exactly one failure: somebody deleting rows. They protect you against nothing else: not a dead machine, not a failed disk, not theft, not fire. |
| No safe, no key escrow | If the appliance dies and the key died with it, every backup you hold is permanently unreadable. There is no recovery path, no vendor override and no brute force. This is the single fastest way to lose the entire clinical record while looking at files that appear to contain it. |

---

## 13. Order checklist

For the CIO and IT lead.

**To order**

- [ ] Mac Studio, M5 Ultra 30C/64C, 96 GB, 1 TB, quantity 1
- [ ] Mac Studio, M5 Max 18C/40C, 64 GB, 1 TB, quantity 1
- [ ] AppleCare+ for Mac Studio, 3-year, quantity 2
- [ ] Rugged 2 TB USB-C SSD, quantity 3
- [ ] UPS, 1500 VA / 1000 W, pure sine, USB HID signalling, quantity 1
- [ ] Managed 8-port gigabit switch, quantity 1
- [ ] 24-inch display, keyboard, mouse, 2-port USB-C KVM, 1 set
- [ ] Lockable cabinet or rack shelf, plus two Kensington-style locks
- [ ] Cat 6 cabling and mounting

**To arrange (hospital, no purchase required)**

- [ ] Locked location identified, with a named physical access list
- [ ] Generator-backed, essential-services power circuit at that location
- [ ] Two wired gigabit drops (appliance and standby), with fixed IP addresses
- [ ] Ward devices confirmed to reach the appliance address
- [ ] Monitors in scope confirmed reachable, read-only, with biomedical staff
      available for the first live test
- [ ] Decision recorded: outbound internet permitted, or fully air-gapped
- [ ] Decision recorded: FileVault on with manual unlock after long outage, or off
- [ ] Hospital safe identified for the key envelope and backup drive B, in
      separate sealed containers
- [ ] Named IT contact for install week and for the restore drill

**Dates, counted from the day we sign**

| Milestone | When |
|---|---|
| Specification issued to hospital | Week 1 |
| Purchase order raised | End of week 3 |
| Delivery expected | Weeks 5 to 7 |
| Install and hardening | Weeks 9 to 10 |
| Key ceremony and restore drill | Weeks 9 to 10 |
| Shadow go-live | Week 13 |

---

## Sign-off

| Role | Name | Signature | Date |
|---|---|---|---|
| Hospital CIO | | | |
| Hospital IT lead | | | |
| Orb (Samuel Morningstar) | | | |

---

## Related documents

| Document | What it covers |
|---|---|
| [PILOT_ROADMAP.md](PILOT_ROADMAP.md) | The 13-week sequence and current status |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Installing and configuring the appliance |
| [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md) | Backup schedule, restore procedure, key escrow |
| [SHADOW_MODE.md](SHADOW_MODE.md) | How the pilot runs once the hardware is in place |
| `perf/` | The soak and latency measurements quoted in section 2 |

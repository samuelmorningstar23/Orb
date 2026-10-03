import urllib.request
import os

os.makedirs("uploads", exist_ok=True)

# Special:FilePath is Wikimedia's stable redirector — it survives the media
# re-hashing that 404'd the previous direct upload.wikimedia.org URLs.
images = [
    ("xray_1.jpg", "https://commons.wikimedia.org/wiki/Special:FilePath/Chest_Xray_PA_3-8-2010.png"),
    ("mri_1.jpg", "https://commons.wikimedia.org/wiki/Special:FilePath/MRI_of_Human_Brain.jpg"),
    ("ct_1.jpg", "https://commons.wikimedia.org/wiki/Special:FilePath/CT_of_a_normal_brain_(thumbnail).png"),
    ("ecg_1.jpg", "https://commons.wikimedia.org/wiki/Special:FilePath/SinusRhythmLabels.svg?width=2000"),
]

for name, url in images:
    path = f"uploads/{name}"
    if not os.path.exists(path):
        print(f"Downloading {name}...")
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response, open(path, 'wb') as out_file:
                out_file.write(response.read())
        except Exception as e:
            print(f"Failed to download {name}: {e}")
    else:
        print(f"{name} already exists.")

print("Mock images ready.")

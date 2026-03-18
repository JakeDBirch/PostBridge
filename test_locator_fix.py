import aaf2
import struct
from urllib.parse import quote

WAV_FILE = r"F:\Blood Trails\MERL Spoof\AAF For Claude\AAF Media\51492eb1-dfcb-7fae-16e2-94490000007c.wav"
SR, CHANNELS, BITS, DURATION = 48000, 1, 24, 4143088
sample_rate = aaf2.rational.AAFRational("48000/1")

def wave_summary(n_ch, s_rate, n_bits):
    blk = n_ch * (n_bits // 8)
    brate = s_rate * blk
    fmt = struct.pack("<HHIIHH", 1, n_ch, s_rate, brate, blk, n_bits)
    hdr = (b"RIFF" + struct.pack("<I", 36) + b"WAVE" +
           b"fmt " + struct.pack("<I", 16) + fmt +
           b"data" + struct.pack("<I", 0))
    return list(hdr)

def file_uri(path):
    fwd = path.replace("\\", "/")
    return "file:///" + "/".join(quote(p, safe="") for p in fwd.split("/"))

def make_loc(f, path):
    loc = f.create.NetworkLocator()
    loc["URLString"].value = file_uri(path)
    return loc

def dump_desc_locators(f, out_path, label):
    with aaf2.open(out_path, "r") as rf:
        for mob in rf.content.mobs:
            if type(mob).__name__ == "SourceMob":
                desc = mob.descriptor
                locs = list(desc.locator)
                print(f"  [{label}] locators found: {len(locs)}")
                for i, loc in enumerate(locs):
                    for prop in loc.properties():
                        try:
                            print(f"    loc[{i}].{prop.name} = {prop.value!r}")
                        except:
                            pass

# --- Test A: desc["Locator"].value = [loc] ---
print("=== Test A: desc[\"Locator\"].value = [loc] ===")
OUT_A = r"F:\PostBridge\PostBridge_Modular\test_locA.aaf"
try:
    with aaf2.open(OUT_A, "w") as f:
        src_mob = f.create.SourceMob()
        src_mob.name = "test"
        f.content.mobs.append(src_mob)
        src_slot = src_mob.create_timeline_slot(sample_rate)
        sc = f.create.SourceClip(media_kind="sound", length=DURATION)
        src_slot.segment = sc
        desc = f.create.WAVEDescriptor()
        desc["Summary"].value = wave_summary(CHANNELS, SR, BITS)
        desc["SampleRate"].value = sample_rate
        desc.length = DURATION
        src_mob.descriptor = desc
        loc = make_loc(f, WAV_FILE)
        desc["Locator"].value = [loc]
        print(f"  After set: desc.locator = {list(desc.locator)}")
    dump_desc_locators(f, OUT_A, "Test A readback")
except Exception as e:
    import traceback; traceback.print_exc()

# --- Test B: desc["Locator"].append(loc) ---
print("\n=== Test B: desc[\"Locator\"].append(loc) ===")
OUT_B = r"F:\PostBridge\PostBridge_Modular\test_locB.aaf"
try:
    with aaf2.open(OUT_B, "w") as f:
        src_mob = f.create.SourceMob()
        src_mob.name = "test"
        f.content.mobs.append(src_mob)
        src_slot = src_mob.create_timeline_slot(sample_rate)
        sc = f.create.SourceClip(media_kind="sound", length=DURATION)
        src_slot.segment = sc
        desc = f.create.WAVEDescriptor()
        desc["Summary"].value = wave_summary(CHANNELS, SR, BITS)
        desc["SampleRate"].value = sample_rate
        desc.length = DURATION
        src_mob.descriptor = desc
        loc = make_loc(f, WAV_FILE)
        try:
            desc["Locator"].append(loc)
            print(f"  After append: desc.locator = {list(desc.locator)}")
        except Exception as e:
            print(f"  desc[\"Locator\"].append failed: {e}")
    dump_desc_locators(f, OUT_B, "Test B readback")
except Exception as e:
    import traceback; traceback.print_exc()

# --- Test C: add loc to mob BEFORE assigning descriptor ---
print("\n=== Test C: build locator list, assign descriptor, set locator ===")
OUT_C = r"F:\PostBridge\PostBridge_Modular\test_locC.aaf"
try:
    with aaf2.open(OUT_C, "w") as f:
        src_mob = f.create.SourceMob()
        src_mob.name = "test"
        f.content.mobs.append(src_mob)
        src_slot = src_mob.create_timeline_slot(sample_rate)
        sc = f.create.SourceClip(media_kind="sound", length=DURATION)
        src_slot.segment = sc
        desc = f.create.WAVEDescriptor()
        desc["Summary"].value = wave_summary(CHANNELS, SR, BITS)
        desc["SampleRate"].value = sample_rate
        desc.length = DURATION
        loc = make_loc(f, WAV_FILE)
        # Add locator BEFORE assigning descriptor to mob
        desc.locator.append(loc)
        print(f"  Before assign: desc.locator = {list(desc.locator)}")
        src_mob.descriptor = desc
        print(f"  After assign: src_mob.descriptor.locator = {list(src_mob.descriptor.locator)}")
    dump_desc_locators(f, OUT_C, "Test C readback")
except Exception as e:
    import traceback; traceback.print_exc()

# --- Test D: use StrongRefVectorProperty directly ---
print("\n=== Test D: inspect StrongRefVectorProperty methods ===")
with aaf2.open(r"F:\PostBridge\PostBridge_Modular\test_locD.aaf", "w") as f:
    desc = f.create.WAVEDescriptor()
    loc = make_loc(f, WAV_FILE)
    prop = desc["Locator"]
    print(f"  type(prop) = {type(prop)}")
    print(f"  dir(prop) = {[x for x in dir(prop) if not x.startswith('_')]}")
    try:
        prop.value = [loc]
        print(f"  After prop.value=[loc]: desc.locator={list(desc.locator)}")
    except Exception as e:
        print(f"  prop.value=[loc] failed: {e}")

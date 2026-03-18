import sys, os

LOG_PATH = r"F:\PostBridge\PostBridge_Modular\aaf_dump.txt"
_log = open(LOG_PATH, "w", encoding="utf-8")

_orig_print = __builtins__["print"] if isinstance(__builtins__, dict) else __builtins__.print
def print(*args, **kwargs):
    _orig_print(*args, **kwargs)
    kwargs.pop("file", None)
    _orig_print(*args, file=_log, **kwargs)
    _log.flush()

print("Script started.")

try:
    import aaf2
    print(f"aaf2 version: {getattr(aaf2, '__version__', 'unknown')}")
except Exception as e:
    print(f"FAILED to import aaf2: {e}")
    _log.close()
    sys.exit(1)

path = r"F:\Blood Trails\20260423_BT_10_Timothy Blythe\01_PROJECT FILES\AAF to XML\20260423_BT_10_Timothy Blythe-02.aaf"

def class_name(obj):
    try:
        return obj.class_name
    except AttributeError:
        return type(obj).__name__

with aaf2.open(path, "r") as f:
    master_names = []
    for mob in f.content.mobs:
        if class_name(mob) == 'MasterMob':
            try: master_names.append(mob.name or '<blank>')
            except: master_names.append('<err>')
    print(f"\n=== MASTERMOB NAMES ({len(master_names)} total) ===")
    for n in sorted(set(master_names))[:80]:
        print(f"  {n!r}")

    print(f"\n=== COMPOSITION MOB SLOTS ===")
    for i, mob in enumerate(f.content.mobs):
        mob_cn = class_name(mob)
        if mob_cn != 'CompositionMob':
            continue
        mob_type = class_name(mob)
        mob_id   = str(mob.mob_id)
        mob_name = getattr(mob, 'name', '<no name>')
        usage    = None
        try:
            usage = mob['AppCode'].value
        except:
            pass
        try:
            usage = mob['UsageCode'].value
        except:
            pass

        print(f"\n{'='*70}")
        print(f"MOB [{i}] type={mob_type}  name={mob_name!r}")
        print(f"  mob_id = {mob_id}")
        if usage:
            print(f"  usage  = {usage}")

        # Print all mob slots
        try:
            for sn, slot in enumerate(mob.slots):
                print(f"  SLOT[{sn}] id={slot.slot_id}  name={slot.name!r}  media_kind={slot.media_kind}")
                seg = slot.segment
                seg_cn = class_name(seg)
                print(f"    segment class = {seg_cn}")
                try:
                    print(f"    segment length = {seg.length}")
                except:
                    pass
                # If sequence, show components (limit to first 5 for brevity)
                if seg_cn == 'Sequence':
                    for ci, comp in enumerate(seg.components):
                        if ci >= 5:
                            print(f"    ... ({sum(1 for _ in seg.components)} total components)")
                            break
                        comp_cn = class_name(comp)
                        print(f"    comp[{ci}] class={comp_cn} length={getattr(comp,'length','?')}", end='')
                        if comp_cn == 'SourceClip':
                            try:
                                print(f" start={comp['StartTime'].value} src_id={comp['SourceID'].value} src_slot={comp['SourceSlotID'].value}", end='')
                            except Exception as e:
                                print(f" (err: {e})", end='')
                        print()
                        # Drill into OperationGroup to show its contents
                        if comp_cn == 'OperationGroup':
                            # Try .segments property
                            try:
                                segs = list(comp.segments)
                                print(f"      .segments -> {len(segs)} items")
                                for si, s in enumerate(segs):
                                    s_cn = class_name(s)
                                    print(f"        seg[{si}] class={s_cn} length={getattr(s,'length','?')}", end='')
                                    if s_cn == 'SourceClip':
                                        try:
                                            print(f" start={s['StartTime'].value} src_id={s['SourceID'].value} src_slot={s['SourceSlotID'].value}", end='')
                                        except Exception as e:
                                            print(f" (err: {e})", end='')
                                    print()
                            except Exception as e:
                                print(f"      .segments ERROR: {e}")
                            # Try ['InputSegments'] property dict
                            try:
                                isegs = list(comp['InputSegments'])
                                print(f"      ['InputSegments'] -> {len(isegs)} items")
                                for si, s in enumerate(isegs):
                                    s_cn = class_name(s)
                                    print(f"        iseg[{si}] class={s_cn}", end='')
                                    if s_cn == 'SourceClip':
                                        try:
                                            print(f" src_id={s['SourceID'].value}", end='')
                                        except: pass
                                    print()
                            except Exception as e:
                                print(f"      ['InputSegments'] ERROR: {e}")
        except Exception as e:
            print(f"  (slots error: {e})")

        # Print descriptor if SourceMob
        if mob_type == 'SourceMob':
            try:
                desc = mob.descriptor
                desc_cn = class_name(desc)
                print(f"  DESCRIPTOR class = {desc_cn}")
                for prop in desc.properties():
                    try:
                        print(f"    {prop.name} = {prop.value!r}")
                    except Exception as e:
                        print(f"    {prop.name} = <err: {e}>")
                # Locators
                try:
                    for li, loc in enumerate(desc.locator):
                        loc_cn = class_name(loc)
                        print(f"  LOCATOR[{li}] class={loc_cn}")
                        for prop in loc.properties():
                            try:
                                print(f"    {prop.name} = {prop.value!r}")
                            except Exception as e:
                                print(f"    {prop.name} = <err: {e}>")
                except Exception as e:
                    print(f"  (locator error: {e})")
            except Exception as e:
                print(f"  (descriptor error: {e})")

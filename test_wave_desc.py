#!/usr/bin/env python3
import aaf2
import struct
from urllib.parse import quote

WAV_FILE = r'F:\Blood Trails\MERL Spoof\AAF For Claude\AAF Media\51492eb1-dfcb-7fae-16e2-94490000007c.wav'
OUT_AAF  = r'F:\PostBridge\PostBridge_Modular\test_wave_desc.aaf'

SR       = 48000
CHANNELS = 1
BITS     = 24
DURATION = 4143088

sample_rate = aaf2.rational.AAFRational('48000/1')

def wave_summary(n_ch, s_rate, n_bits):
    blk   = n_ch * (n_bits // 8)
    brate = s_rate * blk
    fmt   = struct.pack('<HHIIHH', 1, n_ch, s_rate, brate, blk, n_bits)
    hdr   = (b'RIFF' + struct.pack('<I', 36) +
             b'WAVE' + b'fmt ' + struct.pack('<I', 16) + fmt +
             b'data' + struct.pack('<I', 0))
    return list(hdr)

def file_uri(path):
    fwd = path.replace(chr(92), '/')
    encoded = '/'.join(quote(p, safe='') for p in fwd.split('/'))
    return 'file:///' + encoded

print('=== Creating test AAF ===')
try:
    with aaf2.open(OUT_AAF, 'w') as f:
        comp_mob = f.create.CompositionMob('TestSession')
        comp_mob.usage = 'Usage_TopLevel'
        f.content.mobs.append(comp_mob)
        seq = f.create.Sequence(media_kind='sound')
        comp_slot = comp_mob.create_timeline_slot(sample_rate)
        comp_slot.segment = seq
        comp_slot.name = 'A1'

        src_mob = f.create.SourceMob()
        src_mob.name = 'Radio Live Murder Take 2.wav'
        f.content.mobs.append(src_mob)
        src_slot = src_mob.create_timeline_slot(sample_rate)
        src_slot.name = 'A1'
        sc = f.create.SourceClip(media_kind='sound', length=DURATION)
        src_slot.segment = sc
        print('  SourceClip created OK')

        desc = f.create.WAVEDescriptor()
        print('  WAVEDescriptor created OK')
        desc['Summary'].value    = wave_summary(CHANNELS, SR, BITS)
        print('  Summary set OK:', wave_summary(CHANNELS, SR, BITS)[:8])
        desc['SampleRate'].value = sample_rate
        desc.length              = DURATION
        try:
            desc['ContainerFormat'].value = f.dictionary.lookup_containerdef('ContainerDef_AAFKLV')
            print('  ContainerFormat set OK')
        except Exception as e:
            print('  ContainerFormat FAILED:', e)
        src_mob.descriptor = desc
        print('  descriptor assigned OK')

        uri = file_uri(WAV_FILE)
        print('  URI:', uri)
        loc = f.create.NetworkLocator()
        loc['URLString'].value = uri
        src_mob.descriptor.locator.append(loc)
        print('  Locator appended OK')

        master = f.create.MasterMob('Radio Live Murder Take 2.wav')
        master_slot = master.create_timeline_slot(sample_rate)
        master_slot.name = 'A1'
        master_slot.segment = src_mob.create_source_clip(
            slot_id=src_slot.slot_id, media_kind='sound')
        f.content.mobs.append(master)
        print('  MasterMob created OK')

        clip = master.create_source_clip(
            slot_id=master_slot.slot_id, length=DURATION, start=0)
        seq.components.append(clip)
        print('  Clip added to composition OK')

    print('AAF written to:', OUT_AAF)
except Exception as e:
    import traceback
    print('CREATION FAILED:', e)
    traceback.print_exc()

print('')
print('=== DUMP of generated AAF ===')
try:
    with aaf2.open(OUT_AAF, 'r') as f:
        for i, mob in enumerate(f.content.mobs):
            mob_type = type(mob).__name__
            print('MOB[' + str(i) + '] ' + mob_type + '  name=' + repr(mob.name))
            print('  mob_id = ' + str(mob.mob_id))
            if mob_type == 'SourceMob':
                try:
                    desc = mob.descriptor
                    desc_type = type(desc).__name__
                    print('  DESCRIPTOR: ' + desc_type)
                    for prop in desc.properties():
                        try:
                            val = prop.value
                            if isinstance(val, (bytes, bytearray)):
                                val = list(val)
                            print('    ' + prop.name + ' = ' + repr(val))
                        except Exception as e:
                            print('    ' + prop.name + ' = <err:' + str(e) + '>')
                    for li, loc in enumerate(desc.locator):
                        print('  LOCATOR[' + str(li) + ']:')
                        for prop in loc.properties():
                            try:
                                print('    ' + prop.name + ' = ' + repr(prop.value))
                            except Exception as e:
                                print('    ' + prop.name + ' = <err:' + str(e) + '>')
                except Exception as e:
                    print('  descriptor error: ' + str(e))
            for sn, slot in enumerate(mob.slots):
                seg = slot.segment
                length = getattr(seg, 'length', '?')
                print('  SLOT[' + str(sn) + '] id=' + str(slot.slot_id) + ' name=' + repr(slot.name) + ' rate=' + str(slot.edit_rate) + ' seg=' + type(seg).__name__ + ' len=' + str(length))
except Exception as e:
    import traceback
    print('DUMP FAILED:', e)
    traceback.print_exc()

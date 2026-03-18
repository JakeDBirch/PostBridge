import aaf2
import struct
import os
from urllib.parse import quote
from pathlib import Path

WAV_FILE = r"F:\Blood Trails\MERL Spoof\AAF For Claude\AAF Media\51492eb1-dfcb-7fae-16e2-94490000007c.wav"
OUT_AAF  = r"F:\PostBridge\PostBridge_Modular\test_locator_debug.aaf"
SR, CHANNELS, BITS, DURATION = 48000, 1, 24, 4143088
sample_rate = aaf2.rational.AAFRational('48000/1')

def wave_summary(n_ch, s_rate, n_bits):
    blk = n_ch * (n_bits // 8)
    brate = s_rate * blk
    fmt = struct.pack('<HHIIHH', 1, n_ch, s_rate, brate, blk, n_bits)
    hdr = (b'RIFF' + struct.pack('<I', 36) + b'WAVE' +
           b'fmt ' + struct.pack('<I', 16) + fmt +
           b'data' + struct.pack('<I', 0))
    return list(hdr)

def file_uri(path):
    parts = Path(path).parts
    encoded = [quote(p, safe='') for p in parts]
    return 'file:///' + '/'.join(encoded)

print('=== Locator API investigation ===')
with aaf2.open(OUT_AAF, 'w') as f:
    desc = f.create.WAVEDescriptor()
    desc['Summary'].value = wave_summary(CHANNELS, SR, BITS)
    desc['SampleRate'].value = sample_rate
    desc.length = DURATION

    print(f'type(desc.locator) = {type(desc.locator)}')
    print(f'desc.locator = {desc.locator}')
    print(f'id(desc.locator) = {id(desc.locator)}')

    a = desc.locator
    b = desc.locator
    print(f'id(a)==id(b): {id(a)==id(b)}  (False means new list each call - append is lost)')

    loc = f.create.NetworkLocator()
    loc['URLString'].value = file_uri(WAV_FILE)

    print('')
    print('--- Approach 1: desc.locator.append(loc) ---')
    desc.locator.append(loc)
    print(f'  After append: desc.locator = {list(desc.locator)}')

    print('')
    print('--- Approach 2: desc[Locator].value ---')
    try:
        val = desc['Locator'].value
        print(f'  desc[Locator].value type = {type(val)}')
        print(f'  desc[Locator].value = {val}')
        val.append(loc)
        print(f'  After append: {list(desc.locator)}')
    except Exception as e:
        print(f'  Error: {e}')

    print('')
    print('--- All descriptor properties after attempts ---')
    for prop in desc.properties():
        try:
            print(f'  {prop.name} = {prop.value!r}')
        except Exception as e:
            print(f'  {prop.name} = <err:{e}>')

print('')
print('=== Now inspect a WORKING locator (from Premiere AAF) ===')
with aaf2.open(r'F:\Blood Trails\MERL Spoof\AAF For Claude\MERL Spoof.aaf', 'r') as f:
    for mob in f.content.mobs:
        if hasattr(mob, 'descriptor'):
            try:
                desc = mob.descriptor
                if 'WAVE' in type(desc).__name__:
                    print(f'Found WAVEDescriptor on {mob.name!r}')
                    print(f'  type(desc.locator) = {type(desc.locator)}')
                    print(f'  desc.locator = {list(desc.locator)}')
                    a = desc.locator
                    b = desc.locator
                    print(f'  id(a)==id(b): {id(a)==id(b)}')
                    for prop in desc.properties():
                        try:
                            if 'Locator' in prop.name or 'locator' in prop.name:
                                print(f'  PROPERTY: {prop.name} = {prop.value!r}')
                                print(f'  PROPERTY type: {type(prop)}')
                        except:
                            pass
                    break
            except:
                pass

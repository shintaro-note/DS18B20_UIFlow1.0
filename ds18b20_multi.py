# ds18b20_multi.py
# UIFlow (MicroPython 1.11系) 向け DS18B20 拡張ドライバ
#   - 複数センサー対応 (1-Wire ROM Search アルゴリズム + CRC8チェック)
#   - パラサイト電源モード対応 (変換中の強プルアップ)
#
# ビット/バイト単位の1-Wire通信そのものは、UIFlowファームウェアに
# 組み込み済みの _onewire モジュール(Cで実装、正確なタイミング)に
# 委譲しています。Pythonでのビットバンギングはやめました
# (インタプリタの実行オーバーヘッドでタイミングが崩れる問題を回避するため)。
#
# 参考: DS18B20 datasheet (Adafruit copy)
#   https://cdn-learn.adafruit.com/assets/assets/000/130/351/original/DS18B20.pdf
#
# 使い方:
#   from ds18b20_multi import DS18B20
#   sensor = DS18B20(26)          # GPIO26 に接続
#   roms = sensor.scan()          # バス上の全センサーのROMコードを取得(デイジーチェーン対応)
#   sensor.convert_temp()         # 全センサー一斉に変換開始(パラサイト機は自動で強プルアップ)
#   for rom in roms:
#       print(rom.hex(), sensor.read_temp(rom))

import machine
import time
import _onewire


class OneWireError(Exception):
    pass


class OneWire:
    SEARCH_ROM = 0xF0
    READ_ROM = 0x33
    MATCH_ROM = 0x55
    SKIP_ROM = 0xCC

    def __init__(self, pin_no):
        self.pin_no = pin_no
        self.pin = machine.Pin(pin_no, machine.Pin.OPEN_DRAIN, machine.Pin.PULL_UP)
        self.pin.value(1)

    def set_strong_pullup(self, on):
        """パラサイト電源用の強プルアップ。
        変換中に電流を供給する必要があるときだけ push-pull 出力に切り替える。"""
        if on:
            self.pin.init(machine.Pin.OUT)
            self.pin.value(1)
        else:
            self.pin.init(machine.Pin.OPEN_DRAIN, machine.Pin.PULL_UP)
            self.pin.value(1)

    # --- 1-Wire 通信(_onewireモジュールに委譲) ------------------------
    def reset(self):
        return _onewire.reset(self.pin)

    def write_bit(self, b):
        _onewire.writebit(self.pin, b)

    def read_bit(self):
        return _onewire.readbit(self.pin)

    def write_byte(self, byte):
        _onewire.writebyte(self.pin, byte)

    def read_byte(self):
        return _onewire.readbyte(self.pin)

    def write_bytes(self, data):
        for b in data:
            _onewire.writebyte(self.pin, b)

    def read_bytes(self, n):
        return bytes(_onewire.readbyte(self.pin) for _ in range(n))

    @staticmethod
    def crc8(data):
        return _onewire.crc8(bytes(data))

    # --- ROM操作 -------------------------------------------------------------
    def select(self, rom):
        self.reset()
        self.write_byte(self.MATCH_ROM)
        self.write_bytes(rom)

    def skip(self):
        self.write_byte(self.SKIP_ROM)

    def scan(self, check_crc=True):
        """バス上の全デバイスのROMコード(8byte)をリストで返す
        (Maxim AN187準拠のSearch ROMアルゴリズム。前回発見したROMの
        状態を rom バイト配列に保持したまま次の探索を行うのがポイント)

        check_crc=False にすると、ビット化けでCRCが合わなくても
        例外を出さずにそのままROMコードを返す(元のdallas.py同様、
        精度より動作継続を優先する妥協設定)。"""
        devices = []
        rom = bytearray(8)
        last_discrepancy = 0
        last_device = False
        while not last_device:
            if not self.reset():
                break
            self.write_byte(self.SEARCH_ROM)
            last_discrepancy, last_device = self._search_step(rom, last_discrepancy)
            if check_crc and self.crc8(rom[:7]) != rom[7]:
                raise OneWireError("CRC mismatch during ROM search")
            devices.append(bytes(rom))
        return devices

    def _search_step(self, rom, last_discrepancy):
        """rom は前回探索結果を保持したまま渡す(参照渡しでその場で書き換える)。
        戻り値: (今回の last_discrepancy, これが最後のデバイスかどうか)"""
        last_zero = 0
        bit_pos = 1
        for byte_i in range(8):
            for bit_i in range(8):
                mask = 1 << bit_i
                id_bit = self.read_bit()
                cmp_bit = self.read_bit()
                if id_bit and cmp_bit:
                    raise OneWireError("no devices / bus error")
                if not id_bit and not cmp_bit:
                    # 分岐点(2台以上が異なるビットを持つ)
                    if bit_pos < last_discrepancy:
                        # 前回探索した rom の該当ビットをそのまま使う(重要)
                        search_bit = 1 if (rom[byte_i] & mask) else 0
                    elif bit_pos == last_discrepancy:
                        search_bit = 1
                    else:
                        search_bit = 0
                    if search_bit == 0:
                        last_zero = bit_pos
                else:
                    search_bit = id_bit
                if search_bit:
                    rom[byte_i] |= mask
                else:
                    rom[byte_i] &= (~mask) & 0xFF
                self.write_bit(search_bit)
                bit_pos += 1
        last_device = (last_zero == 0)
        return last_zero, last_device


class DS18B20:
    CONVERT_T = 0x44
    READ_SCRATCH = 0xBE
    READ_POWER_SUPPLY = 0xB4
    FAMILY_CODES = (0x10, 0x28)  # 0x10=DS18S20, 0x28=DS18B20/DS1822

    def __init__(self, pin_no):
        self.ow = OneWire(pin_no)
        self.roms = []
        self._parasite = {}

    def scan(self, check_crc=True):
        """バスをスキャンし、DS18B20系センサーのROMコード一覧を返す。
        check_crc=False で、ビット化けによる例外を無視して続行する。"""
        self.roms = [r for r in self.ow.scan(check_crc=check_crc) if r[0] in self.FAMILY_CODES]
        self._parasite = {rom: self._read_parasite_flag(rom) for rom in self.roms}
        return self.roms

    def is_parasite(self, rom):
        return self._parasite.get(rom, False)

    def _read_parasite_flag(self, rom):
        self.ow.select(rom)
        self.ow.write_byte(self.READ_POWER_SUPPLY)
        return self.ow.read_bit() == 0  # 0 = パラサイト電源

    def convert_temp(self, rom=None, wait_ms=750):
        """温度変換を開始する。rom=Noneなら全センサー一斉変換(Skip ROM)。
        パラサイト機が1台でもあれば変換中は自動で強プルアップをかける。"""
        if rom is None:
            self.ow.reset()
            self.ow.skip()
            need_pullup = any(self._parasite.values())
        else:
            self.ow.select(rom)
            need_pullup = self.is_parasite(rom)

        self.ow.write_byte(self.CONVERT_T)

        if need_pullup:
            self.ow.set_strong_pullup(True)
            time.sleep_ms(wait_ms)
            self.ow.set_strong_pullup(False)
        else:
            time.sleep_ms(wait_ms)

    def read_temp(self, rom, check_crc=True):
        self.ow.select(rom)
        self.ow.write_byte(self.READ_SCRATCH)
        data = self.ow.read_bytes(9)
        if check_crc and self.ow.crc8(data[:8]) != data[8]:
            import ubinascii
            raise OneWireError("CRC mismatch (rom=%s)" % ubinascii.hexlify(rom))
        raw = (data[1] << 8) | data[0]
        if raw & 0x8000:
            raw -= 0x10000
        if rom[0] == 0x10:  # DS18S20は9bit分解能
            return raw / 2
        return raw / 16  # DS18B20 / DS1822 (デフォルト12bit)

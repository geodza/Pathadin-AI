"""Local file chooser. Windows uses the OS dialog directly; no Tk installation needed."""
import ctypes
import json
import os
import sys


def windows_file(dialog=None, error_code=None):
    from ctypes import wintypes as w

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ('lStructSize', w.DWORD), ('hwndOwner', w.HWND), ('hInstance', w.HINSTANCE),
            ('lpstrFilter', w.LPCWSTR), ('lpstrCustomFilter', w.LPWSTR),
            ('nMaxCustFilter', w.DWORD), ('nFilterIndex', w.DWORD),
            ('lpstrFile', ctypes.POINTER(ctypes.c_wchar)), ('nMaxFile', w.DWORD),
            ('lpstrFileTitle', w.LPWSTR), ('nMaxFileTitle', w.DWORD),
            ('lpstrInitialDir', w.LPCWSTR), ('lpstrTitle', w.LPCWSTR),
            ('Flags', w.DWORD), ('nFileOffset', w.WORD), ('nFileExtension', w.WORD),
            ('lpstrDefExt', w.LPCWSTR), ('lCustData', ctypes.c_ssize_t),
            ('lpfnHook', ctypes.c_void_p), ('lpTemplateName', w.LPCWSTR),
            ('pvReserved', ctypes.c_void_p), ('dwReserved', w.DWORD), ('FlagsEx', w.DWORD),
        ]

    filename = ctypes.create_unicode_buffer(32768)
    request = OPENFILENAMEW()
    request.lStructSize = ctypes.sizeof(request)
    request.lpstrFile = filename
    request.nMaxFile = len(filename)
    request.lpstrFilter = 'Whole-slide images\0*.jpg;*.jpeg;*.png;*.bmp;*.webp;*.svs;*.mrxs;*.ndpi;*.scn;*.tif;*.tiff;*.bif;*.vms;*.vmu\0All files\0*.*\0\0'
    request.nFilterIndex = 1
    request.lpstrTitle = 'Pathadin AI — Select a whole-slide image'
    request.Flags = 0x00080000 | 0x00001000 | 0x00000800 | 0x00000008 | 0x02000000
    if dialog is None:
        library = ctypes.WinDLL('comdlg32', use_last_error=True)
        dialog = library.GetOpenFileNameW
        dialog.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
        dialog.restype = w.BOOL
        error_code = library.CommDlgExtendedError
        error_code.restype = w.DWORD
    if dialog(ctypes.pointer(request)):
        return filename.value
    code = error_code() if error_code else 0
    if code:
        raise RuntimeError(f'Windows file dialog failed (0x{code:08X}). Paste the full slide path instead.')
    return ''  # Cancel is not an error.


def choose_file():
    if os.name == 'nt':
        return windows_file()
    import tkinter as tk
    from tkinter.filedialog import askopenfilename
    root = tk.Tk()
    root.withdraw()
    try:
        return askopenfilename(title='Open whole-slide image',filetypes=[
            ('Whole-slide images','*.jpg *.jpeg *.png *.bmp *.webp *.svs *.mrxs *.ndpi *.scn *.tif *.tiff *.bif'),('All files','*')])
    finally:
        root.destroy()


if __name__ == '__main__':
    try:
        result = {'path':choose_file()}
    except Exception as exc:
        result = {'error':str(exc)}
    sys.stdout.buffer.write(json.dumps(result,ensure_ascii=False).encode('utf-8'))

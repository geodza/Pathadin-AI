import ctypes
import unittest
from pathadinai.picker import windows_file


class PickerTests(unittest.TestCase):
    def test_selection_preserves_unicode_and_spaces(self):
        expected = 'C:\\Slides\\Näide case.svs'
        def select(request):
            source = ctypes.create_unicode_buffer(expected)
            ctypes.memmove(request.contents.lpstrFile,source,ctypes.sizeof(source))
            return 1
        self.assertEqual(windows_file(select),expected)

    def test_cancel_is_empty_path(self):
        self.assertEqual(windows_file(lambda request:0,lambda:0),'')

    def test_native_error_is_reported(self):
        with self.assertRaisesRegex(RuntimeError,'00003003'):
            windows_file(lambda request:0,lambda:0x3003)

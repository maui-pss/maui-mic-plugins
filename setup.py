#!/usr/bin/env python

import os, sys
import glob
from distutils.core import setup
try:
    import setuptools
    # enable "setup.py develop", optional
except ImportError:
    pass

MOD_NAME = 'mauimic'

version_path = 'VERSION'
if not os.path.isfile(version_path):
    print 'No VERSION file in topdir, abort'
    sys.exit(1)

try:
    # first line should be the version number
    version = open(version_path).readline().strip()
    if not version:
        print 'VERSION file is invalid, abort'
        sys.exit(1)

    ver_file = open('%s/__version__.py' % MOD_NAME, 'w')
    ver_file.write("VERSION = \"%s\"\n" % version)
    ver_file.close()
except IOError:
    print 'WARNING: Cannot write version number file'

# --install-layout is recognized after 2.5
if sys.version_info[:2] > (2, 5):
    if len(sys.argv) > 1 and 'install' in sys.argv:
        try:
            import platform
            (dist, ver, id) = platform.linux_distribution()

            # for debian-like distros, mods will be installed to
            # ${PYTHONLIB}/dist-packages
            if dist in ('debian', 'Ubuntu'):
                sys.argv.append('--install-layout=deb')
        except:
            pass

PACKAGES = [MOD_NAME,
            MOD_NAME + '/imager',
           ]

IMAGER_PLUGINS = glob.glob(os.path.join("plugins", "imager", "*.py"))

# the following code to do a simple parse for '--prefix' opts
prefix = sys.prefix
is_next = False
for arg in sys.argv:
    if is_next:
        prefix = arg
        break
    if '--prefix=' in arg:
        prefix = arg[9:]
        break
    elif '--prefix' == arg:
        is_next = True

os.environ['PREFIX'] = prefix
setup(name=MOD_NAME,
      version = version,
      description = 'Maui plugins for mic',
      author='Pier Luigi Fiorini',
      author_email='pierluigi.fiorini@gmail.com',
      url='https://github.com/mauios/maui-mic-plugins',
      packages = PACKAGES,
      data_files = [("%s/lib/mic/plugins/imager" % prefix, IMAGER_PLUGINS)]
)

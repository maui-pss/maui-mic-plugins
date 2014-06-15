#!/usr/bin/python -tt
#
# Copyright (c) 2014 Pier Luigi Fiorini
# Copyright (c) 2011 Intel, Inc.
# Copyright (c) 2007-2012, Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation; version 2 of the License
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc., 59
# Temple Place - Suite 330, Boston, MA 02111-1307, USA.

import os, sys
import glob
import shutil

from mic import kickstart, msger
from mic.utils import fs_related, rpmmisc, runner, misc
from mic.utils.errors import CreatorError
from mic.imager.loop import LoopImageCreator
from mauimic.fs import *

class LiveImageCreatorBase(LoopImageCreator):
    """A base class for LiveCD image creators.

        This class serves as a base class for the architecture-specific LiveCD
        image creator subclass, LiveImageCreator.

        LiveImageCreator creates a bootable ISO containing the system image,
        bootloader, bootloader configuration, kernel and initramfs.
    """

    def __init__(self, creatoropts=None, pkgmgr=None,
                 title="Linux", product="Linux"):
        """Initialise a LiveImageCreator instance.

           This method takes the same arguments as ImageCreator.__init__().
        """
        LoopImageCreator.__init__(self, creatoropts, pkgmgr)

        # Controls whether to use squashfs to compress the image.
        self.skip_compression = False

        # Controls whether an image minimizing snapshot should be created.
        #
        # This snapshot can be used when copying the system image from the ISO in
        # order to minimize the amount of data that needs to be copied; simply,
        # it makes it possible to create a version of the image's filesystem with
        # no spare space.
        self.skip_minimize = False

        # A flag which indicates i act as a convertor default false
        self.actasconvertor = False

        # The bootloader timeout from kickstart
        if self.ks:
            self._timeout = kickstart.get_timeout(self.ks, 10)
        else:
            self._timeout = 10

        # The default kernel type from kickstart
        if self.ks:
            self._default_kernel = kickstart.get_default_kernel(self.ks,
                                                                "kernel")
        else:
            self._default_kernel = None

        if self.ks:
            parts = kickstart.get_partitions(self.ks)
            if len(parts) > 1:
                raise CreatorError("Can't support multi partitions in ks file "
                                   "for this image type")
            # FIXME: rename rootfs img to self.name,
            # else can't find files when create iso
            self._instloops[0]['name'] = self.name + ".img"

        self.__isodir = None

        self.__modules = ["=ata", "sym53c8xx", "aic7xxx", "=usb", "=firewire",
                          "=mmc", "=pcmcia", "mptsas", "udf", "virtio_blk",
                          "virtio_pci", "virtio_scsi", "virtio_net", "virtio_mmio",
                          "virtio_balloon", "virtio-rng"]
        if self.ks:
            self.__modules.extend(kickstart.get_modules(self.ks))

        self._dep_checks.extend(["isohybrid",
                                 "unsquashfs",
                                 "mksquashfs",
                                 "dd",
                                 "genisoimage"])

        self._isofstype = "iso9660"
        self.base_on = False

        self.title = title
        self.product = product

    #
    # Hooks for subclasses
    #
    def _configure_bootloader(self, isodir):
        """Create the architecture specific booloader configuration.

            This is the hook where subclasses must create the booloader
            configuration in order to allow a bootable ISO to be built.

            isodir -- the directory where the contents of the ISO are to
                      be staged
        """
        raise CreatorError("Bootloader configuration is arch-specific, "
                           "but not implemented for this arch!")

    def _get_menu_options(self):
        """Return a menu options string for syslinux configuration.
        """
        if self.ks is None:
            return "liveinst autoinst"
        r = kickstart.get_menu_args(self.ks)
        return r

    def _get_kernel_options(self):
        """Return a kernel options string for bootloader configuration.

            This is the hook where subclasses may specify a set of kernel
            options which should be included in the images bootloader
            configuration.

            A sensible default implementation is provided.
        """

        default = "ro quiet rd.live.image rd.luks=0 rd.md=0 rd.dm=0"

        if self.ks is None:
            r = default
        else:
            r = kickstart.get_kernel_args(self.ks, default)

        if (os.path.exists(self._instroot + "/usr/bin/plymouth") or \
            os.path.exists(self._instroot + "/usr/bin/ply-image")) and \
           ' splash' not in r:
            r += ' splash'

        return r

    def _get_mkisofs_options(self, isodir):
        """Return the architecture specific mkisosfs options.

            This is the hook where subclasses may specify additional arguments
            to mkisofs, e.g. to enable a bootable ISO to be built.

            By default, an empty list is returned.
        """
        return []

    #
    # Helpers for subclasses
    #
    def _has_checkisomd5(self):
        """Check whether checkisomd5 is available in the install root."""
        def exists(instroot, path):
            return os.path.exists(instroot + path)

        if (exists(self._instroot, "/usr/lib/anaconda-runtime/checkisomd5") or
            exists(self._instroot, "/usr/bin/checkisomd5")):
            return True

        return False

    def _mount_instroot(self, base_on = None):
        LoopImageCreator._mount_instroot(self, base_on)
        self.__write_initrd_conf(self._instroot + "/etc/sysconfig/mkinitrd")
        self.__write_dracut_conf(self._instroot + "/etc/dracut.conf.d/02livecd.conf")

    def _unmount_instroot(self):
        self.__restore_file(self._instroot + "/etc/sysconfig/mkinitrd")
        self.__restore_file(self._instroot + "/etc/dracut.conf.d/02livecd.conf")
        LoopImageCreator._unmount_instroot(self)

    def __ensure_isodir(self):
        if self.__isodir is None:
            self.__isodir = self._mkdtemp("iso-")
        return self.__isodir

    def _get_isodir(self):
        return self.__ensure_isodir()

    def _set_isodir(self, isodir = None):
        self.__isodir = isodir

    def _create_bootconfig(self):
        """Configure the image so that it's bootable."""
        self._configure_bootloader(self.__ensure_isodir())

    def _get_post_scripts_env(self, in_chroot):
        env = LoopImageCreator._get_post_scripts_env(self, in_chroot)

        if not in_chroot:
            env["LIVE_ROOT"] = self.__ensure_isodir()

        return env

    def __extra_filesystems(self):
        return "vfat msdos isofs ext4 xfs btrfs"

    def __extra_drivers(self):
        retval = "sr_mod sd_mod ide-cd cdrom "
        for module in self.__modules:
            if module == "=usb":
                retval = retval + "ehci_hcd uhci_hcd ohci_hcd "
                retval = retval + "usb_storage usbhid "
            elif module == "=firewire":
                retval = retval + "firewire-sbp2 firewire-ohci "
                retval = retval + "sbp2 ohci1394 ieee1394 "
            elif module == "=mmc":
                retval = retval + "mmc_block sdhci sdhci-pci "
            elif module == "=pcmcia":
                retval = retval + "pata_pcmcia "
            else:
                retval = retval + module + " "
        return retval

    def __restore_file(self,path):
        try:
            os.unlink(path)
        except:
            pass
        if os.path.exists(path + '.rpmnew'):
            os.rename(path + '.rpmnew', path)

    def __write_initrd_conf(self, path):
        if not os.path.exists(os.path.dirname(path)):
            makedirs(os.path.dirname(path))
        f = open(path, "a")
        f.write('LIVEOS="yes"\n')
        f.write('PROBE="no"\n')
        f.write('MODULES+="' + self.__extra_filesystems() + '"\n')
        f.write('MODULES+="' + self.__extra_drivers() + '"\n')
        f.close()

    def __write_dracut_conf(self, path):
        if not os.path.exists(os.path.dirname(path)):
            makedirs(os.path.dirname(path))
        f = open(path, "a")
        f.write('filesystems+="' + self.__extra_filesystems() + ' "\n')
        f.write('drivers+="' + self.__extra_drivers() + ' "\n')
        f.write('add_dracutmodules+=" dmsquash-live pollcdrom "\n')
        f.write('hostonly="no"\n')
        f.write('dracut_rescue_image="no"\n')
        f.close()

    def __create_iso(self, isodir):
        iso = self._outdir + "/" + self.name + ".iso"
        genisoimage = fs_related.find_binary_path("genisoimage")
        args = [genisoimage,
                "-J", "-r",
                "-hide-rr-moved", "-hide-joliet-trans-tbl",
                "-V", self.fslabel,
                "-o", iso]

        args.extend(self._get_mkisofs_options(isodir))

        args.append(isodir)

        if runner.show(args) != 0:
            raise CreatorError("ISO creation failed!")

        """ It should be ok still even if you haven't isohybrid """
        isohybrid = None
        try:
            isohybrid = fs_related.find_binary_path("isohybrid")
        except:
            pass

        if isohybrid:
            args = [isohybrid, "-partok", iso ]
            if runner.show(args) != 0:
             	raise CreatorError("Hybrid ISO creation failed!")

        self.__implant_md5sum(iso)

    def __implant_md5sum(self, iso):
        """Implant an isomd5sum."""
        if os.path.exists("/usr/bin/implantisomd5"):
            implantisomd5 = "/usr/bin/implantisomd5"
        elif os.path.exists("/usr/lib/anaconda-runtime/implantisomd5"):
            implantisomd5 = "/usr/lib/anaconda-runtime/implantisomd5"
        else:
            msger.warning("isomd5sum not installed; not setting up mediacheck")
            implantisomd5 = ""
            return

        runner.show([implantisomd5, iso])

    def _stage_final_image(self):
        try:
            fs_related.makedirs(self.__ensure_isodir() + "/LiveOS")

            minimal_size = self._resparse()

            if not self.skip_minimize:
                fs_related.create_image_minimizer(self.__isodir + \
                                                      "/LiveOS/osmin.img",
                                                  self._image,
                                                  minimal_size)

            if self.skip_compression:
                shutil.move(self._image, self.__isodir + "/LiveOS/ext3fs.img")
            else:
                fs_related.makedirs(os.path.join(
                                        os.path.dirname(self._image),
                                        "LiveOS"))
                shutil.move(self._image,
                            os.path.join(os.path.dirname(self._image),
                                         "LiveOS", "ext3fs.img"))
                fs_related.mksquashfs(os.path.dirname(self._image),
                           self.__isodir + "/LiveOS/squashfs.img")

            self.__create_iso(self.__isodir)

            if self.pack_to:
                isoimg = os.path.join(self._outdir, self.name + ".iso")
                packimg = os.path.join(self._outdir, self.pack_to)
                misc.packing(packimg, isoimg)
                os.unlink(isoimg)

        finally:
            shutil.rmtree(self.__isodir, ignore_errors = True)
            self.__isodir = None

class x86LiveImageCreator(LiveImageCreatorBase):
    """ImageCreator for x86 machines"""
    def __init__(self, *args, **kwargs):
        LiveImageCreatorBase.__init__(self, *args, **kwargs)
        self._efiarch = None

    def _get_mkisofs_options(self, isodir):
        return [ "-b", "isolinux/isolinux.bin",
                 "-c", "isolinux/boot.cat",
                 "-no-emul-boot", "-boot-info-table",
                 "-boot-load-size", "4" ]

    def _get_required_packages(self):
        return ["syslinux", "syslinux-extlinux"] + \
               LiveImageCreatorBase._get_required_packages(self)

    def _get_isolinux_stanzas(self, isodir):
        return ""

    def __find_syslinux_menu(self):
        for menu in ["vesamenu.c32", "menu.c32"]:
            for dir in ("/usr/lib/syslinux/", "/usr/share/syslinux/"):
                if os.path.isfile(self._instroot + dir + menu):
                    return menu

        raise CreatorError("syslinux not installed : "
                           "no suitable *menu.c32 found")

    def __find_syslinux_mboot(self):
        #
        # We only need the mboot module if we have any xen hypervisors
        #
        if not glob.glob(self._instroot + "/boot/xen.gz*"):
            return None

        return "mboot.c32"

    def __copy_syslinux_files(self, isodir, menu, mboot = None):
        files = ["isolinux.bin", "ldlinux.c32", "libcom32.c32", "libutil.c32", menu]
        if mboot:
            files += [mboot]

        for f in files:
            if os.path.exists(self._instroot + "/usr/lib/syslinux/" + f):
                path = self._instroot + "/usr/lib/syslinux/" + f
            elif os.path.exists(self._instroot + "/usr/share/syslinux/" + f):
                path = self._instroot + "/usr/share/syslinux/" + f
            if not os.path.isfile(path):
                raise CreatorError("syslinux not installed : "
                                   "%s not found" % path)

            shutil.copy(path, isodir + "/isolinux/")

    def __copy_syslinux_background(self, isodest):
        background_path = self._instroot + \
                          "/usr/share/anaconda/boot/syslinux-vesa-splash.png"

        if not os.path.exists(background_path):
            # fallback to F13 location
            background_path = self._instroot + \
                              "/usr/lib/anaconda-runtime/syslinux-vesa-splash.png"

            if not os.path.exists(background_path):
                return False

        shutil.copyfile(background_path, isodest)

        return True

    def __copy_kernel_and_initramfs(self, isodir, version, index):
        bootdir = self._instroot + "/boot"

        shutil.copyfile(bootdir + "/vmlinuz-" + version,
                        isodir + "/isolinux/vmlinuz" + index)

        isDracut = False
        if os.path.exists(bootdir + "/initramfs-" + version + ".img"):
            shutil.copyfile(bootdir + "/initramfs-" + version + ".img",
                            isodir + "/isolinux/initrd" + index + ".img")
            isDracut = True
        elif os.path.exists(bootdir + "/initrd-" + version + ".img"):
            shutil.copyfile(bootdir + "/initrd-" + version + ".img",
                            isodir + "/isolinux/initrd" + index + ".img")
        else:
            msger.error("No initrd or initramfs found for %s" % (version,))

        is_xen = False
        if os.path.exists(bootdir + "/xen.gz-" + version[:-3]):
            shutil.copyfile(bootdir + "/xen.gz-" + version[:-3],
                            isodir + "/isolinux/xen" + index + ".gz")
            is_xen = True

        return (is_xen, isDracut)

    def __is_default_kernel(self, kernel, kernels):
        if len(kernels) == 1:
            return True

        if kernel == self._default_kernel:
            return True

        if kernel.startswith("kernel-") and kernel[7:] == self._default_kernel:
            return True

        return False

    def __get_basic_syslinux_config(self, **args):
        return """
default %(menu)s
timeout %(timeout)d

menu background %(background)s
menu autoboot Starting %(title)s in # second{,s}. Press any key to interrupt.

menu clear
menu title %(title)s
menu width 78
menu margin 4
menu rows 7
menu vshift 10
menu tabmsgrow 14
menu cmdlinerow 14
menu helpmsgrow 16
menu helpmsgendrow 29

# Refer to http://syslinux.zytor.com/wiki/index.php/Doc/menu

menu color border * #00000000 #00000000 none
menu color sel 0 #ff3a6496 #00000000 none
menu color title 0 #ff7ba3d0 #00000000 none
menu color tabmsg 0 #ff3a6496 #00000000 none
menu color unsel 0 #ff347ead #00000000 none
menu color hotsel 0 #ff64b0ea #00000000 none
menu color hotkey 0 #ffffffff #00000000 none
menu color help 0 #993677bc #00000000 none
menu color scrollbar 0 #ffffffff #ff355594 none
menu color timeout 0 #ff999999 #00000000 none
menu color timeout_msg 0 #ff444b54 #00000000 none
menu color cmdmark 0 #844bb2e5 #00000000 none
menu color cmdline 0 #ffffffff #00000000 none

menu tabmsg Press Tab for full configuration options on menu items.
""" % args

    def __get_image_stanza(self, is_xen, isDracut, **args):
        if isDracut:
            args["rootlabel"] = "live:CDLABEL=%(fslabel)s" % args
        else:
            args["rootlabel"] = "CDLABEL=%(fslabel)s" % args

        if not is_xen:
            template = """label %(short)s
  menu label %(long)s
  kernel vmlinuz%(index)s
  append initrd=initrd%(index)s.img root=%(rootlabel)s rootfstype=%(isofstype)s %(liveargs)s %(extra)s
"""
        else:
            template = """label %(short)s
  menu label %(long)s
  kernel mboot.c32
  append xen%(index)s.gz --- vmlinuz%(index)s root=%(rootlabel)s rootfstype=%(isofstype)s %(liveargs)s %(extra)s --- initrd%(index)s.img
"""

        if args.get("help"):
            template += """  text help
      %(help)s
  endtext
"""

        return template % args

    def __get_image_stanzas(self, isodir):
        kernels = self._get_kernel_versions()
        kernel_options = self._get_kernel_options()
        checkisomd5 = self._has_checkisomd5()

        # Stanzas for insertion into the config template
        linux = []
        basic = []
        check = []

        index = "0"
        for kernel, version in ((k,v) for k in kernels for v in kernels[k]):
            (is_xen, isDracut) = self.__copy_kernel_and_initramfs(isodir, version, index)
            if index == "0":
                self._isDracut = isDracut

            default = self.__is_default_kernel(kernel, kernels)

            if default:
                long = self.product
            elif kernel.startswith("kernel-"):
                long = "%s (%s)" % (self.product, kernel[7:])
            else:
                long = "%s (%s)" % (self.product, kernel)

            # tell dracut not to ask for LUKS passwords or activate mdraid sets
            if isDracut:
                kern_opts = kernel_options + " rd.luks=0 rd.md=0 rd.dm=0"
            else:
                kern_opts = kernel_options

            linux.append(self.__get_image_stanza(is_xen, isDracut,
                                           fslabel = self.fslabel,
                                           isofstype = "auto",
                                           liveargs = kern_opts,
                                           long = "^Start " + long,
                                           short = "linux" + index,
                                           extra = "",
                                           help = "",
                                           index = index))

            if default:
                linux[-1] += "  menu default\n"

            basic.append(self.__get_image_stanza(is_xen, isDracut,
                                           fslabel = self.fslabel,
                                           isofstype = "auto",
                                           liveargs = kern_opts,
                                           long = "Start " + long + " in ^basic graphics mode.",
                                           short = "basic" + index,
                                           extra = "nomodeset",
                                           help = "Try this option out if you're having trouble starting.",
                                           index = index))

            if checkisomd5:
                check.append(self.__get_image_stanza(is_xen, isDracut,
                                               fslabel = self.fslabel,
                                               isofstype = "auto",
                                               liveargs = kern_opts,
                                               long = "^Test this media & start " + long,
                                               short = "check" + index,
                                               extra = "rd.live.check",
                                               help = "",
                                               index = index))
            else:
                check.append(None)

            index = str(int(index) + 1)

        return (linux, basic, check)

    def __get_memtest_stanza(self, isodir):
        memtest = glob.glob(self._instroot + "/boot/memtest86*")
        if not memtest:
            return ""

        shutil.copyfile(memtest[0], isodir + "/isolinux/memtest")

        return """label memtest
  menu label Run a ^memory test
  text help
    If your system is having issues, an problem with your
    system's memory may be the cause. Use this utility to
    see if the memory is working correctly.
  endtext
  kernel memtest
"""

    def __get_local_stanza(self, isodir):
        return """label local
  menu label Boot from ^local drive
  localboot 0xffff
"""

    def _configure_syslinux_bootloader(self, isodir):
        """configure the boot loader"""
        makedirs(isodir + "/isolinux")

        menu = self.__find_syslinux_menu()

        self.__copy_syslinux_files(isodir, menu,
                                   self.__find_syslinux_mboot())

        background = ""
        if self.__copy_syslinux_background(isodir + "/isolinux/splash.png"):
            background = "splash.png"

        cfg = self.__get_basic_syslinux_config(menu = menu,
                                               background = background,
                                               title = self.title,
                                               timeout = self._timeout * 10)
        cfg += "menu separator\n"

        linux, basic, check = self.__get_image_stanzas(isodir)
        # Add linux stanzas to main menu
        for s in linux:
            cfg += s
        cfg += "menu separator\n"

        cfg += """menu begin ^Troubleshooting
  menu title Troubleshooting
"""
        # Add basic video and check to submenu
        for b, c in zip(basic, check):
            cfg += b
            if c:
                cfg += c

        cfg += self.__get_memtest_stanza(isodir)
        cfg += "menu separator\n"

        cfg += self.__get_local_stanza(isodir)
        cfg += self._get_isolinux_stanzas(isodir)

        cfg += """menu separator
label returntomain
  menu label Return to ^main menu.
  menu exit
menu end
"""
        cfgf = open(isodir + "/isolinux/isolinux.cfg", "w")
        cfgf.write(cfg)
        cfgf.close()

    @property
    def efiarch(self):
        if not self._efiarch:
            # for most things, we want them named boot$efiarch
            efiarch = {"i386": "IA32", "x86_64": "X64"}
            self._efiarch = efiarch[rpmmisc.getBaseArch()]
        return self._efiarch

    def __copy_efi_files(self, isodir):
        """ Copy the efi files into /EFI/BOOT/
            If any of them are missing, return False.
            requires:
              shim.efi
              gcdx64.efi
              fonts/unicode.pf2
        """
        fail = False
        missing = []
        files = [("/boot/efi/EFI/*/shim.efi", "/EFI/BOOT/BOOT%s.efi" % (self.efiarch,)),
                 ("/boot/efi/EFI/*/gcdx64.efi", "/EFI/BOOT/grubx64.efi"),
                 ("/boot/efi/EFI/*/fonts/unicode.pf2", "/EFI/BOOT/fonts/"),
                ]
        makedirs(isodir+"/EFI/BOOT/fonts/")
        for src, dest in files:
            src_glob = glob.glob(self._instroot + src)
            if not src_glob:
                missing.append("Missing EFI file (%s)" % (src,))
                fail = True
            else:
                shutil.copy(src_glob[0], isodir+dest)
        map(msger.error, missing)
        return fail

    def __get_basic_efi_config(self, **args):
        return """
set default="1"

function load_video {
  insmod efi_gop
  insmod efi_uga
  insmod video_bochs
  insmod video_cirrus
  insmod all_video
}

load_video
set gfxpayload=keep
insmod gzio
insmod part_gpt
insmod ext2

set timeout=%(timeout)d
### END /etc/grub.d/00_header ###

search --no-floppy --set=root -l '%(isolabel)s'

### BEGIN /etc/grub.d/10_linux ###
""" %args

    def __get_efi_image_stanza(self, **args):
        if self._isDracut:
            args["rootlabel"] = "live:LABEL=%(fslabel)s" % args
        else:
            args["rootlabel"] = "CDLABEL=%(fslabel)s" % args
        return """menuentry '%(long)s' --class fedora --class gnu-linux --class gnu --class os {
	linuxefi /isolinux/vmlinuz%(index)s root=%(rootlabel)s %(liveargs)s %(extra)s
	initrdefi /isolinux/initrd%(index)s.img
}
""" %args

    def __get_efi_image_stanzas(self, isodir, name):
        # FIXME: this only supports one kernel right now...

        kernel_options = self._get_kernel_options()
        checkisomd5 = self._has_checkisomd5()

        cfg = ""

        for index in range(0, 9):
            # we don't support xen kernels
            if os.path.exists("%s/EFI/BOOT/xen%d.gz" %(isodir, index)):
                continue
            cfg += self.__get_efi_image_stanza(fslabel = self.fslabel,
                                               liveargs = kernel_options,
                                               long = "Start " + self.product,
                                               extra = "", index = index)
            if checkisomd5:
                cfg += self.__get_efi_image_stanza(fslabel = self.fslabel,
                                                   liveargs = kernel_options,
                                                   long = "Test this media & start " + self.product,
                                                   extra = "rd.live.check",
                                                   index = index)
            cfg += """
submenu 'Troubleshooting -->' {
"""
            cfg += self.__get_efi_image_stanza(fslabel = self.fslabel,
                                               liveargs = kernel_options,
                                               long = "Start " + self.product + " in basic graphics mode",
                                               extra = "nomodeset", index = index)

            cfg+= """}
"""
            break

        return cfg

    def _configure_efi_bootloader(self, isodir):
        """Set up the configuration for an EFI bootloader"""
        if self.__copy_efi_files(isodir):
            shutil.rmtree(isodir + "/EFI")
            msger.warning("Failed to copy EFI files, no EFI Support will be included.")
            return

        cfg = self.__get_basic_efi_config(isolabel = self.fslabel,
                                          timeout = self._timeout)
        cfg += self.__get_efi_image_stanzas(isodir, self.name)

        cfgf = open(isodir + "/EFI/BOOT/grub.cfg", "w")
        cfgf.write(cfg)
        cfgf.close()

        # first gen mactel machines get the bootloader name wrong apparently
        if rpmUtils.arch.getBaseArch() == "i386":
            os.link(isodir + "/EFI/BOOT/BOOT%s.efi" % (self.efiarch),
                    isodir + "/EFI/BOOT/BOOT.efi")

    def _configure_bootloader(self, isodir):
        self._configure_syslinux_bootloader(isodir)
        # TODO: Enable EFI configuration when we actually have grub2
        #self._configure_efi_bootloader(isodir)

arch = rpmmisc.getBaseArch()
if arch in ("i386", "x86_64"):
    LiveCDImageCreator = x86LiveImageCreator
elif arch.startswith("arm"):
    LiveCDImageCreator = LiveImageCreatorBase

else:
    raise CreatorError("Architecture not supported!")

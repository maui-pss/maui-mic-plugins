#!/usr/bin/python -tt
#
# Copyright (c) 2011 Intel, Inc.
# Copyright (c) 2012 Jolla Ltd.
# Copyright (c) 2014 Pier Luigi Fiorini
# Contact: Pier Luigi Fiorini <pierluigi.fiorini@gmail.com>
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

import os
import shutil
import tempfile

from mic import chroot, msger, rt_util
from mic.utils import misc, fs_related, errors, common
from mic.conf import configmgr
import mauimic.imager.livecd as livecd
from mic.plugin import pluginmgr
from mic.pluginbase import ImagerPlugin

class LiveCDPlugin(ImagerPlugin):
    name = 'maui_livecd'

    @classmethod
    def do_create(self, subcmd, opts, *args):
        """${cmd_name}: create livecd image for Maui

        Usage:
            ${name} ${cmd_name} <ksfile> [OPTS]

        ${cmd_option_list}
        """

        creatoropts = common.creatoropts(args)

        if creatoropts['arch'] and creatoropts['arch'].startswith('arm'):
            msger.warning('maui_livecd cannot support arm images, Quit')
            return

        creator = livecd.LiveCDImageCreator(creatoropts, creatoropts['pkgmgr_pcls'])
        creator._recording_pkgs = creatoropts['record_pkgs']

        self.check_image_exists(creator.destdir,
                                creator.pack_to,
                                [creator.name + ".iso"],
                                creatoropts['release'])

        try:
            creator.check_depend_tools()
            creator.mount(None, creatoropts["cachedir"])
            creator.install()
            creator.configure(creatoropts["repomd"])
            creator.copy_kernel()
            creator.unmount()
            creator.package(creatoropts["outdir"])
            if creatoropts['release'] is not None:
                creator.release_output(ksconf, creatoropts['outdir'], creatoropts['release'])
            else:
                creator.outimage.append(creatoropts['dst_ks'])

            creator.print_outimage_info()

        except errors.CreatorError:
            raise
        finally:
            creator.cleanup()

        msger.info("Finished.")
        return 0

    @classmethod
    def do_chroot(cls, target):
        os_image = cls.do_unpack(target)
        os_image_dir = os.path.dirname(os_image)

        # unpack image to target dir
        imgsize = misc.get_file_size(os_image) * 1024L * 1024L
        imgtype = misc.get_image_type(os_image)
        if imgtype == "btrfsimg":
            fstype = "btrfs"
            myDiskMount = fs_related.BtrfsDiskMount
        elif imgtype in ("ext3fsimg", "ext4fsimg"):
            fstype = imgtype[:4]
            myDiskMount = fs_related.ExtDiskMount
        else:
            raise errors.CreatorError("Unsupported filesystem type: %s" % fstype)

        extmnt = misc.mkdtemp()
        extloop = myDiskMount(fs_related.SparseLoopbackDisk(os_image, imgsize),
                              extmnt,
                              fstype,
                              4096,
                              "%s label" % fstype)
        try:
            extloop.mount()

        except errors.MountError:
            extloop.cleanup()
            shutil.rmtree(extmnt, ignore_errors = True)
            shutil.rmtree(os_image_dir, ignore_errors = True)
            raise

        try:
            envcmd = fs_related.find_binary_inchroot("env", extmnt)
            if envcmd:
                cmdline = "%s HOME=/root /bin/bash" % envcmd
            else:
                cmdline = "/bin/bash"
            chroot.chroot(extmnt, None, cmdline)
        except:
            raise errors.CreatorError("Failed to chroot to %s." %target)
        finally:
            chroot.cleanup_after_chroot("img", extloop, os_image_dir, extmnt)

    @classmethod
    def do_pack(cls, base_on):
        import subprocess

        def __mkinitrd(instance):
            kernelver = instance._get_kernel_versions().values()[0][0]
            dracut_modules = "dmsquash-live pollcdrom drm" #systemd systemd-bootchart
            dracut_drivers = "sr_mod sd_mod ide-cd cdrom ehci_hcd uhci_hcd ohci_hcd usb_storage usbhid"
            args = [
                "/usr/bin/dracut", "-f", "-N", "/boot/initrd-%s.img" % kernelver,
                "--add", dracut_modules.split(" "),
                "--add-drivers", dracut_drivers.split(" "),
                kernelver
            ]
            try:
                subprocess.call(args, preexec_fn = instance._chroot)
            except OSError, (err, msg):
               raise errors.CreatorError("Failed to execute /usr/bin/dracut: %s" % msg)

        def __run_post_cleanups(instance):
            kernelver = instance._get_kernel_versions().values()[0][0]
            args = ["rm", "-f", "/boot/initrd-%s.img" % kernelver]

            try:
                subprocess.call(args, preexec_fn = instance._chroot)
            except OSError, (err, msg):
               raise errors.CreatorError("Failed to run post cleanups: %s" % msg)

        convertoropts = configmgr.convert
        convertoropts['name'] = os.path.splitext(os.path.basename(base_on))[0]
        convertor = livecd.LiveCDImageCreator(convertoropts)
        imgtype = misc.get_image_type(base_on)
        if imgtype == "btrfsimg":
            fstype = "btrfs"
        elif imgtype in ("ext3fsimg", "ext4fsimg"):
            fstype = imgtype[:4]
        else:
            raise errors.CreatorError("Unsupported filesystem type: %s" % fstype)
        convertor._set_fstype(fstype)
        try:
            convertor.mount(base_on)
            __mkinitrd(convertor)
            convertor._create_bootconfig()
            __run_post_cleanups(convertor)
            convertor.launch_shell(convertoropts['shell'])
            convertor.unmount()
            convertor.package()
            convertor.print_outimage_info()
        finally:
            shutil.rmtree(os.path.dirname(base_on), ignore_errors = True)

    @classmethod
    def do_unpack(cls, srcimg):
        img = srcimg
        imgmnt = misc.mkdtemp()
        imgloop = fs_related.DiskMount(fs_related.LoopbackDisk(img, 0), imgmnt)
        try:
            imgloop.mount()
        except errors.MountError:
            imgloop.cleanup()
            raise

        # legacy LiveOS filesystem layout support, remove for F9 or F10
        if os.path.exists(imgmnt + "/squashfs.img"):
            squashimg = imgmnt + "/squashfs.img"
        else:
            squashimg = imgmnt + "/LiveOS/squashfs.img"

        tmpoutdir = misc.mkdtemp()
        # unsquashfs requires outdir mustn't exist
        shutil.rmtree(tmpoutdir, ignore_errors = True)
        misc.uncompress_squashfs(squashimg, tmpoutdir)

        try:
            # legacy LiveOS filesystem layout support, remove for F9 or F10
            if os.path.exists(tmpoutdir + "/os.img"):
                os_image = tmpoutdir + "/os.img"
            else:
                os_image = tmpoutdir + "/LiveOS/ext3fs.img"

            if not os.path.exists(os_image):
                raise errors.CreatorError("'%s' is not a valid live CD ISO : neither "
                                          "LiveOS/ext3fs.img nor os.img exist" %img)

            imgname = os.path.basename(srcimg)
            imgname = os.path.splitext(imgname)[0] + ".img"
            rtimage = os.path.join(tempfile.mkdtemp(dir = "/var/tmp", prefix = "tmp"), imgname)
            shutil.copyfile(os_image, rtimage)

        finally:
            imgloop.cleanup()
            shutil.rmtree(tmpoutdir, ignore_errors = True)
            shutil.rmtree(imgmnt, ignore_errors = True)

        return rtimage

#!/bin/sh

# vim: noexpandtab ts=8 sw=4 softtabstop=4

# Setup a semi-sane environment
PATH=/sbin:/bin:/usr/sbin:/usr/bin:/usr/local/sbin:/usr/local/bin:/rescue
export PATH
HOME=/root
export HOME
TERM=${TERM:-cons25}
export TERM

. /etc/avatar.conf

is_truenas()
{

    test "$AVATAR_PROJECT" = "TrueNAS"
    return $?
}

do_sata_dom()
{

    if ! is_truenas ; then
	return 1
    fi
    install_sata_dom_prompt
    return $?
}

get_product_path()
{
    echo /cdrom /.mount
}

get_image_name()
{
    find $(get_product_path) -name "$AVATAR_PROJECT-$AVATAR_ARCH.img.xz" -type f
}

# Convert /etc/version* to /etc/avatar.conf
#
# 1 - old /etc/version* file
# 2 - dist version of avatar.conf
# 3 - destination avatar.conf
upgrade_version_to_avatar_conf()
{
    local destconf srcconf srcversion
    local project version revision arch

    srcversion=$1
    srcconf=$2
    destconf=$3

    set -- $(sed -E -e 's/-amd64/-x64/' -e 's/-i386/-x86/' -e 's/(.*)-([^-]+) \((.*)\)/\1-\3-\2/' -e 's/-/ /' -e 's/-([^-]+)$/ \1/' -e 's/-([^-]+)$/ \1/' < $srcversion)

    project=$1
    version=$2
    revision=$3
    arch=$4

    sed \
        -e "s,^AVATAR_ARCH=\".*\",AVATAR_ARCH=\"$arch\",g" \
        -e "s,^AVATAR_BUILD_NUMBER=\".*\"\$,AVATAR_BUILD_NUMBER=\"$revision\",g" \
        -e "s,^AVATAR_PROJECT=\".*\"\$,AVATAR_PROJECT=\"$project\",g" \
        -e "s,^AVATAR_VERSION=\".*\"\$,AVATAR_VERSION=\"$version\",g" \
        < $srcconf > $destconf.$$

    mv $destconf.$$ $destconf
}

build_config_old()
{
    # build_config ${_disk} ${_image} ${_config_file}

    local _disk=$1
    local _image=$2
    local _config_file=$3

    cat << EOF > "${_config_file}"
# Added to stop pc-sysinstall from complaining
installMode=fresh
installInteractive=no
installType=FreeBSD
installMedium=dvd
packageType=tar

disk0=${_disk}
partition=image
image=${_image}
bootManager=bsd
commitDiskPart
EOF
}
build_config()
{
    # build_config ${_disk} ${_image} ${_config_file}

    local _disk=$1
    local _image=$2
    local _config_file=$3

    cat << EOF > "${_config_file}"
# Added to stop pc-sysinstall from complaining
installMode=fresh
installInteractive=no
installType=FreeBSD
installMedium=dvd
packageType=tar

disk0=${_disk}
partscheme=GPT
partition=all
bootManager=bsd
commitDiskPart
EOF
}

wait_keypress()
{
    local _tmp
    read -p "Press ENTER to continue." _tmp
}

sort_disklist()
{

    sed 's/\([^0-9]*\)/\1 /' | sort +0 -1 +1n | tr -d ' '
}

# return 0 if no raid devices, or !0 if there are some.
get_raid_present()
{
	local _cnt
	local _dummy

	if [ ! -d "/dev/raid" ] ; then
		return 0;
	fi

	_cnt=0
	ls /dev/raid/ > /tmp/raidfiles
	while read _dummy ; do _cnt=$(($_cnt + 1));done < /tmp/raidfiles
	return $_cnt
}

get_physical_disks_list()
{
    VAL=`sysctl -n kern.disks`

    get_raid_present
    if [ $? -ne 0 ] ; then
	VAL="$VAL `cd /dev ; ls -d raid/* | grep -v '[0-9][a-z]'`"
    fi

    VAL=`echo $VAL | tr ' ' '\n'| grep -v '^cd' | sort_disklist`
    export VAL
}

get_media_description()
{
    local _media
    local _description
    local _cap

    _media=$1
    VAL=""
    if [ -n "${_media}" ]; then
        _description=`pc-sysinstall disk-list -c |grep "^${_media}"\
            | awk -F':' '{print $2}'|sed -E 's|.*<(.*)>.*$|\1|'`
	# if pc-sysinstall doesn't know anything about the device
	# (raid drives) then fill in for it.
	if [ -z "$_description" ] ; then
		_description="Unknown Device"
	fi
        _cap=`diskinfo ${_media} | awk '{
            capacity = $3;
            if (capacity >= 1099511627776) {
                printf("%.1f TiB", capacity / 1099511627776.0);
            } else if (capacity >= 1073741824) {
                printf("%.1f GiB", capacity / 1073741824.0);
            } else if (capacity >= 1048576) {
                printf("%.1f MiB", capacity / 1048576.0);
            } else {
                printf("%d Bytes", capacity);
        }}'`
        VAL="${_description} -- ${_cap}"
    fi
    export VAL
}

disk_is_mounted()
{
    local _dev

    _dev="/dev/$1"
    mount -v|grep -qE "^${_dev}[sp][0-9]+"
    return $?
}


new_install_verify()
{
    local _type="$1"
    local _disk="$2"
    local _tmpfile="/tmp/msg"
    cat << EOD > "${_tmpfile}"
WARNING:
- This will erase ALL partitions and data on ${_disk}.
- You can't use ${_disk} for sharing data.

NOTE:
- Installing on flash media is preferred to installing on a
  hard drive.

Proceed with the ${_type}?
EOD
    _msg=`cat "${_tmpfile}"`
    rm -f "${_tmpfile}"
    dialog --title "$AVATAR_PROJECT ${_type}" --yesno "${_msg}" 13 74
    [ $? -eq 0 ] || exit 1
}

ask_upgrade()
{
    local _disk="$1"
    local _tmpfile="/tmp/msg"
    cat << EOD > "${_tmpfile}"
Upgrading the installation will preserve your existing configuration.

Do you wish to perform an upgrade or a fresh installation on ${_disk}?
EOD
    _msg=`cat "${_tmpfile}"`
    rm -f "${_tmpfile}"
    dialog --title "Upgrade this $AVATAR_PROJECT installation" --no-label "Fresh Install" --yes-label "Upgrade Install" --yesno "${_msg}" 8 74
    return $?
}

mount_disk() {
	local _disk
	local _mnt

	if [ $# -ne 2 ]; then
		return 1
	fi

	_disk="$1"
	_mnt="$2"
	mkdir -p "${_mnt}"
	mount -t zfs -o noatime freenas-boot/ROOT/default ${_mnt}
	mkdir -p ${_mnt}/boot/grub
	mount -t ufs -o noatime /dev/${_disk}p2 ${_mnt}/boot/grub
	mkdir -p ${_mnt}/data
	return 0
}
	
partition_disk() {
	local _disk

	if [ $# -ne 1 ]; then
	    false
	else
	    _disk=$1
	fi

	gpart destroy -F ${_disk} || true
	gpart create -s gpt ${_disk}
	# For grub
	gpart add -t bios-boot -s 512k ${_disk}
	gpart add -t freebsd-ufs -s 32m ${_disk}
	# The rest of the disk
	gpart add -t freebsd-zfs -a 4k ${_disk}

	newfs -L "grub" /dev/${_disk}p2
	zpool create -f -o cachefile=/tmp/zpool.cache -o version=28 -O mountpoint=none -O atime=off -O canmount=off freenas-boot ${_disk}p3
	zfs create -o canmount=off freenas-boot/ROOT
	zfs create -o mountpoint=legacy freenas-boot/ROOT/default

	return 0
}

disk_is_freenas()
{
    local _disk="$1"
    local _rv=1
    local upgrade_style=""
    local os_part=""
    local data_part=""

    # We have two kinds of potential upgrades here.
    # The old kind, with 4 slices, and the new kind,
    # with two partitions.

    mkdir -p /tmp/data_old
    if [ -c /dev/${_disk}s4 ]; then
	os_part=/dev/${_disk}s1a
	data_part=/dev/${_disk}s4
	upgrade_style="old"
    elif [ -c /dev/${_disk}p2 ]; then
	os_part=/dev/${_disk}p3
	data_part=/dev/${_disk}p2
	upgrade_style="new"
    else
	return 1
    fi

    if [ "${upgrade_style}" = "new" ]; then
	# We're going to be very lazy, and just
	# use zdb to see if there is a freenas-boot
	# pool on it
	if zdb -l ${os_part} | grep "name: 'freenas-boot'" > /dev/null ; then
	    return 0
	fi
	# If it's partitioned this way, and isn't freenas-boot, then
	# it's not freenas.
	return 1
    fi

    # Everything below here is for old style upgrade.
    if ! mount "${data_part}" /tmp/data_old ; then
	return 1
    fi

    ls /tmp/data_old > /tmp/data_old.ls
    if [ -f /tmp/data_old/freenas-v1.db ]; then
        _rv=0
    fi
    # XXX side effect, shouldn't be here!
    cp -pR /tmp/data_old/. /tmp/data_preserved
    umount /tmp/data_old
    if [ $_rv -eq 0 ]; then
	mount ${os_part} /tmp/data_old
        # ah my old friend, the can't see the mount til I access the mountpoint
        # bug
        ls /tmp/data_old > /dev/null
	if [ -f /tmp/data_old/conf/base/etc/hostid ]; then
	    cp -p /tmp/data_old/conf/base/etc/hostid /tmp/
	fi
        if [ -d /tmp/data_old/root/.ssh ]; then
            cp -pR /tmp/data_old/root/.ssh /tmp/
        fi
        if [ -d /tmp/data_old/boot/modules ]; then
            mkdir -p /tmp/modules
            for i in `ls /tmp/data_old/boot/modules`
            do
                cp -p /tmp/data_old/boot/modules/$i /tmp/modules/
            done
        fi
        if [ -d /tmp/data_old/usr/local/fusionio ]; then
            cp -pR /tmp/data_old/usr/local/fusionio /tmp/
        fi
	if [ -f /tmp/data_old/boot.config ]; then
	    cp /tmp/data_old/boot.config /tmp/
	fi
	if [ -f /tmp/data_old/boot/loader.conf.local ]; then
	    cp /tmp/data_old/boot/loader.conf.local /tmp/
	fi
        umount /tmp/data_old
    fi
    rmdir /tmp/data_old
    return $_rv
}

menu_install()
{
    local _action
    local _disklist
    local _tmpfile
    local _answer
    local _cdlist
    local _items
    local _disk
    local _disk_old
    local _config_file
    local _desc
    local _list
    local _msg
    local _satadom
    local _i
    local _do_upgrade
    local _menuheight
    local _msg
    local _dlv
    local os_part
    local data_part
    local upgrade_style

    local readonly CD_UPGRADE_SENTINEL="/data/cd-upgrade"
    local readonly NEED_UPDATE_SENTINEL="/data/need-update"
    local readonly POOL="freenas-boot"

    _tmpfile="/tmp/answer"
    TMPFILE=$_tmpfile

    if do_sata_dom
    then
	_satadom="YES"
    else
	_satadom=""
	get_physical_disks_list
	_disklist="${VAL}"

	_list=""
	_items=0
	for _disk in ${_disklist}; do
	    get_media_description "${_disk}"
	    _desc="${VAL}"
	    _list="${_list} ${_disk} '${_desc}'"
	    _items=$((${_items} + 1))
	done

	_tmpfile="/tmp/answer"
	if [ ${_items} -ge 10 ]; then
	    _items=10
	    _menuheight=20
	else
	    _menuheight=8
	    _menuheight=$((${_menuheight} + ${_items}))
	fi
	if [ "${_items}" -eq 0 ]; then
		# Inform the user
		eval "dialog --title 'Choose destination media' --msgbox 'No drives available' 5 60" 2>${_tmpfile}
		return 0
	fi
	eval "dialog --title 'Choose destination media' \
	      --menu 'Select the drive where $AVATAR_PROJECT should be installed.' \
	      ${_menuheight} 60 ${_items} ${_list}" 2>${_tmpfile}
	[ $? -eq 0 ] || exit 1

    fi # ! do_sata_dom

    _disk=`cat "${_tmpfile}"`
    rm -f "${_tmpfile}"

    # Debugging seatbelts
    if disk_is_mounted "${_disk}" ; then
        dialog --msgbox "The destination drive is already in use!" 6 74
        exit 1
    fi

    _do_upgrade=0
    _action="installation"
    if disk_is_freenas ${_disk} ; then
        if ask_upgrade ${_disk} ; then
            _do_upgrade=1
            _action="upgrade"
        fi
	if [ -c /dev/${_disk}s4 ]; then
	    upgrade_style="old"
	elif [ -c /dev/${_disk}p2 ]; then
	    upgrade_style="new"
	else
	    echo "Unknown upgrade style" 1>&2
	fi
    elif [ ${_satadom} -a -c /dev/ufs/TrueNASs4 ]; then
	# Special hack for USB -> DOM upgrades
	_disk_old=`glabel status | grep ' ufs/TrueNASs4 ' | awk '{ print $3 }' | sed -e 's,s4$,,g'`
	if disk_is_freenas ${_disk_old} ; then
	    if ask_upgrade ${_disk_old} ; then
		_do_upgrade=2
		_action="upgrade"
	    fi
	fi
    fi
    new_install_verify "$_action" ${_disk}
    _config_file="/tmp/pc-sysinstall.cfg"

    # Start critical section.
    trap "set +x; read -p \"The $AVATAR_PROJECT $_action on $_disk has failed. Press any key to continue.. \" junk" EXIT
    set -e
    set -x

    #  _disk, _image, _config_file
    # we can now build a config file for pc-sysinstall
    build_config  ${_disk} "$(get_image_name)" ${_config_file}

    # For one of the install_worker ISO scripts
    export INSTALL_MEDIA=${_disk}
    if [ ${_do_upgrade} -eq 1 ]
    then
        /etc/rc.d/dmesg start
        mkdir -p /tmp/data
	if [ "${upgrade_style}" = "old" ]; then
		mount /dev/${_disk}s1a /tmp/data
	elif [ "${upgrade_style}" = "new" ]; then
		zpool import -f -N ${POOL}
		mount -t zfs -o noatime freenas-boot/ROOT/default /tmp/data
	else
		echo "Unknown upgrade style" 1>&2
		false
	fi
	# XXX need to find out why
	ls /tmp/data > /dev/null
        # pre-avatar.conf build. Convert it!
        if [ ! -e /tmp/data/conf/base/etc/avatar.conf ]
        then
            upgrade_version_to_avatar_conf \
		    /tmp/data/conf/base/etc/version* \
		    /etc/avatar.conf \
		    /tmp/data/conf/base/etc/avatar.conf
        fi

	# This needs to be rewritten.
        install_worker.sh -D /tmp/data -m / pre-install
        umount /tmp/data
        rmdir /tmp/data
	if [ -c /dev/${_disk}s4 ]; then
	    # Destroy the partition for old-style upgrades
	    gpart destroy -F ${_disk} || true
	fi
    else
        # Run through some sanity checks on new installs ;).. some of the
        # checks won't make sense, but others might (e.g. hardware sanity
        # checks).
        install_worker.sh -D / -m / pre-install

	# Destroy existing partition table, if there is any but tolerate
	# failure.
	gpart destroy -F ${_disk} || true
    fi

    # Run pc-sysinstall against the config generated

    # Hack #1
    export ROOTIMAGE=1
    # Hack #2
    ls $(get_product_path) > /dev/null

    if [ ${_do_upgrade} -ne 0 ]; then
	# For new-style upgrades -- we have a ${_disk}p2 --
	# we don't need to back up the data, just touch the
	# sentinal files.
	if [ "${upgrade_style}" = "old" ]; then
		partition_disk ${_disk}
	fi
	mount_disk ${_disk} /tmp/data

	# XXX if we have preserved /data
	if [ "${upgrade_style}" = "old" ]; then
		cp -pR /tmp/data_preserved/ /tmp/data/data
	fi
	# Tell it to look in /.mount (with an implied /Packages) for the packages.
	/usr/local/bin/freenas-install -P /.mount/FreeNAS -M /.mount/FreeNAS-MANIFEST /tmp/data

	rm -f /tmp/data/conf/default/etc/fstab /tmp/data/conf/base/etc/fstab
	echo "/dev/${_disk}p2	/boot/grub	ufs	rw,noatime	1	1" > /tmp/data/etc/fstab
	ln /tmp/data/etc/fstab /tmp/data/conf/base/etc/fstab || echo "Cannot link fstab"
	if [ -f /tmp/hostid ]; then
            cp -p /tmp/hostid /tmp/data/conf/base/etc
	fi
        if [ -d /tmp/.ssh ]; then
            cp -pR /tmp/.ssh /tmp/data/root/
        fi

	# TODO: this needs to be revisited.
        if [ -d /tmp/modules ]; then
            for i in `ls /tmp/modules`
            do
                cp -p /tmp/modules/$i /tmp/data/boot/modules
            done
        fi
        if [ -d /tmp/fusionio ]; then
            cp -pR /tmp/fusionio /tmp/data/usr/local/
        fi
	if [ -f /tmp/boot.config ]; then
	    cp /tmp/boot.config /tmp/data/
	fi
	if [ -f /tmp/loader.conf.local ]; then
	    cp /tmp/loader.conf.local /tmp/data/boot/
	fi
	sed -i '' -e 's,^module_path=.*,module_path="/boot/kernel;/boot/modules;/usr/local/modules;",g' /tmp/data/boot/loader.conf /tmp/data/boot/loader.conf.local

        if is_truenas ; then
            install_worker.sh -D /tmp/data -m / install
        fi

	# Debugging pause.
	# read foo

	# XXX: Fixup
	tar cf - -C /tmp/data/conf/base etc | tar xf - -C /tmp/data/
	mount -t devfs devfs /tmp/data/dev

	# Create a temporary /var
	mount -t tmpfs tmpfs /tmp/data/var
	chroot /tmp/data /usr/sbin/mtree -deUf /etc/mtree/BSD.var.dist -p /var

	# Set default boot filesystem
	zpool set bootfs=freenas-boot/ROOT/default freenas-boot

	# Install grub
	chroot /tmp/data/ /sbin/zpool set cachefile=/boot/zfs/zpool.cache freenas-boot
	chroot /tmp/data/ /etc/rc.d/ldconfig start
	chroot /tmp/data/ /usr/bin/sed -i.bak -e 's,^ROOTFS=.*$,ROOTFS=freenas-boot/ROOT/default,g' /usr/local/sbin/beadm /usr/local/etc/grub.d/10_ktrueos
	mkdir /tmp/bakup
	mv /tmp/data/usr/local/etc/grub.d/10_ktrueos.bak /tmp/bakup
	chroot /tmp/data/ /usr/local/sbin/grub-install --modules='zfs part_gpt' /dev/${_disk}
	chroot /tmp/data/ /usr/local/sbin/beadm activate default
	chroot /tmp/data/ /usr/local/sbin/grub-mkconfig -o /boot/grub/grub.cfg > /dev/null 2>&1
	chroot /tmp/data/ /bin/mv /usr/local/sbin/beadm.bak /usr/local/sbin/beadm
	mv /tmp/bakup/10_ktrueos.bak /tmp/data/usr/local/etc/grub.d/10_ktrueos

	dialog --msgbox "The installer has preserved your database file.
$AVATAR_PROJECT will migrate this file, if necessary, to the current format." 6 74
    else
#	/rescue/pc-sysinstall -c ${_config_file}

	partition_disk ${_disk}
	mount_disk ${_disk} /tmp/data

#	# Copy the initial databases over
	cp -R /data/* /tmp/data/data
	chown -R www:www /tmp/data/data
	/usr/local/bin/freenas-install -P /.mount/FreeNAS -M /.mount/FreeNAS-MANIFEST /tmp/data
	rm -f /tmp/data/etc/fstab /tmp/data/conf/base/etc/fstab
	echo "/dev/${_disk}p2	/boot/grub	ufs	rw,noatime	1	1" > /tmp/data/etc/fstab
	ln /tmp/data/etc/fstab /tmp/data/conf/base/etc/fstab || echo "Cannot link fstab"

	mount -t devfs devfs /tmp/data/dev

	# Create a temporary /var
	mount -t tmpfs tmpfs /tmp/data/var
	chroot /tmp/data /usr/sbin/mtree -deUf /etc/mtree/BSD.var.dist -p /var

	# Set default boot filesystem
	zpool set bootfs=freenas-boot/ROOT/default freenas-boot

	# Install grub
	chroot /tmp/data/ /sbin/zpool set cachefile=/boot/zfs/zpool.cache freenas-boot
	chroot /tmp/data/ /etc/rc.d/ldconfig start
	chroot /tmp/data/ /usr/bin/sed -i.bak -e 's,^ROOTFS=.*$,ROOTFS=freenas-boot/ROOT/default,g' /usr/local/sbin/beadm /usr/local/etc/grub.d/10_ktrueos
	chroot /tmp/data/ /usr/local/sbin/grub-install --modules='zfs part_gpt' /dev/${_disk}
	chroot /tmp/data/ /usr/local/sbin/beadm activate default
	chroot /tmp/data/ /usr/local/sbin/grub-mkconfig -o /boot/grub/grub.cfg > /dev/null 2>&1
	chroot /tmp/data/ /bin/mv /usr/local/sbin/beadm.bak /usr/local/sbin/beadm
	chroot /tmp/data/ /bin/mv /usr/local/etc/grub.d/10_ktrueos.bak /usr/local/etc/grub.d/10_ktrueos

	set +x
    fi

    umount /tmp/data/boot/grub
    umount /tmp/data/dev
    umount /tmp/data/var
    umount /tmp/data/

    if is_truenas ; then
	# This is not going to work at all right now.
	# We'd need to create a partition earlier.
	false
        # Put a swap partition on newly created installation image
        if [ -e /dev/${_disk}s3 ]; then
            gpart delete -i 3 ${_disk}
            gpart add -t freebsd ${_disk}
            echo "/dev/${_disk}s3.eli		none			swap		sw		0	0" > /tmp/fstab.swap
        fi

        mkdir -p /tmp/data
        mount /dev/${_disk}s4 /tmp/data
        ls /tmp/data > /dev/null
        mv /tmp/fstab.swap /tmp/data/
        umount /tmp/data
        rmdir /tmp/data
    fi

    # End critical section.
    set +e

    trap - EXIT

    _msg="The $AVATAR_PROJECT $_action on ${_disk} succeeded!\n"
    _dlv=`/sbin/sysctl -n vfs.nfs.diskless_valid 2> /dev/null`
    if [ ${_dlv:=0} -ne 0 ]; then
        _msg="${_msg}Please reboot, and change BIOS boot order to *not* boot over network."
    else
        _msg="${_msg}Please remove the CDROM and reboot."
    fi
    dialog --msgbox "$_msg" 6 74

    return 0
}

menu_shell()
{
    /bin/sh
}

menu_reboot()
{
    echo "Rebooting..."
    reboot >/dev/null
}

menu_shutdown()
{
    echo "Halting and powering down..."
    halt -p >/dev/null
}

#
# Use the following kernel environment variables
#
#  test.nfs_mount -  The NFS directory to mount to get
#                    access to the test script.
#  test.script    -  The path of the test script to run,
#                    relative to the NFS mount directory.
#  test.run_tests_on_boot - If set to 'yes', then run the
#                           tests on bootup, before displaying
#                           the install menu. 
#
#  For example, if the following variables are defined:
#
#    test.nfs_mount=10.5.0.24:/usr/jails/pxeserver/tests
#    test.script=/tests/run_tests.sh
#
#  Then the system will execute the following:
#     mount -t nfs 10.5.0.24:/usr/jails/pxeserver/tests /tmp/tests
#     /tmp/tests/tests/run_tests.sh
menu_test()
{
    local _script
    local _nfs_mount

    _script="$(kenv test.script 2> /dev/null)"
    _nfs_mount="$(kenv test.nfs_mount 2> /dev/null)"
    if [ -z "$_script" -o -z "$_nfs_mount"  ]; then
        return
    fi
  
    if [ -e /tmp/tests ]; then
        umount /tmp/tests 2> /dev/null
        rm -fr /tmp/tests
    fi 
    mkdir -p /tmp/tests
    if [ ! -d /tmp/tests ]; then
        echo "No test directory"
        wait_keypress
    fi
    umount /tmp/tests 2> /dev/null
    mount -t nfs -o ro "$_nfs_mount" /tmp/tests
    if [ ! -e "/tmp/tests/$_script" ]; then
        echo "Cannot find /tmp/tests/$_script"
        wait_keypress
        return
    fi

    dialog --stdout --prgbox /tmp/tests/$_script 15 80
}

main()
{
    local _tmpfile="/tmp/answer"
    local _number
    local _test_option=

    case "$(kenv test.run_tests_on_boot 2> /dev/null)" in
    [Yy][Ee][Ss])
        menu_test
        ;;
    esac

    if [ -n "$(kenv test.script 2> /dev/null)" ]; then
        _test_option="5 Test"
    fi

    while :; do

        dialog --clear --title "$AVATAR_PROJECT $AVATAR_VERSION Console Setup" --menu "" 12 73 6 \
            "1" "Install/Upgrade" \
            "2" "Shell" \
            "3" "Reboot System" \
            "4" "Shutdown System" \
            $_test_option \
            2> "${_tmpfile}"
        _number=`cat "${_tmpfile}"`
        case "${_number}" in
            1) menu_install ;;
            2) menu_shell ;;
            3) menu_reboot ;;
            4) menu_shutdown ;;
            5) menu_test ;;
        esac
    done
}

if is_truenas ; then
    . "$(dirname "$0")/install_sata_dom.sh"
fi

main

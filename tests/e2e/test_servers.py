"""More than one NAS, and an encrypted one."""

import os


def test_one_folder_backed_up_to_two_nases(world, make_samba):
    """3-2-1: the same folder to two NASes left at the same default host name;
    each sync touches only its own."""
    home = make_samba(hostname="raspberrypi")
    offsite = make_samba(hostname="raspberrypi")
    folder = world.pair("photos", {"img1.jpg": "pixels 1"})
    world.config.remove_pair("photos", server="lab")
    for name, box in (("home", home), ("offsite", offsite)):
        world.nasync("add", folder, "photos", "--name", "photos", "--server", name, check=1)
        world.config.add_or_update_server(name, "127.0.0.1", "labuser", "labpass",
                                          "backup", box["port"], root="nas")
        world.nasync("add", folder, "photos", "--name", "photos", "--server", name, check=0)

    world.nasync("sync", "--server", "home", check=0)
    world.nasync("sync", "--server", "offsite", check=0)
    world.write("photos", "img2.jpg", "pixels 2")
    world.nasync("sync", "--server", "home", check=0)

    assert world.samba_files(home, "nas/photos") == ["img1.jpg", "img2.jpg"]
    assert world.samba_files(offsite, "nas/photos") == ["img1.jpg"], \
        "syncing one NAS changed the other"


def test_an_encrypted_connection_carries_a_sync(world, make_samba):
    secure = make_samba(hostname="vault", globals_=["smb encrypt = required"])
    world.config.add_or_update_server("secure", "127.0.0.1", "labuser", "labpass",
                                      "backup", secure["port"], root="enc")
    folder = world.local("secret")
    os.makedirs(folder)
    world.write("secret", "taxes.pdf", os.urandom(100_000))
    world.nasync("encryption", "on", "--server", "secure", check=0)
    world.nasync("add", folder, "secret", "--name", "secret", "--server", "secure", check=0)
    world.nasync("sync", "--server", "secure", check=0)
    assert world.samba_files(secure, "enc/secret") == ["taxes.pdf"]
    world.nasync("server-info", "--server", "secure", check=0)
    assert "encrypt" in world.last_output.lower(), world.last_output

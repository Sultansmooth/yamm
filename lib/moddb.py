from storage import ModEntry, ModDependency, ModService, db
import json
import urllib
import logging
from collections import defaultdict

import hashlib

hasher = hashlib.sha256
BLOCKSIZE = 65536

def create_filehash(path):
    """
    Return Base64 hash of file at path
    """
    h = hasher()
    with open(path, "rb") as f:
        buf = f.read(BLOCKSIZE)
        while len(buf) > 0:
            h.update(buf)
            buf = f.read(BLOCKSIZE)
    return h.digest().encode("base64").strip().strip("=")

L = logging.getLogger("moddb")

class ModDependencies(object):
    def __init__(self):
        self.depmap = defaultdict(list)

    def add_dependency(self, tag, provider, relation, source):
        self.depmap[tag].append((provider, source, relation))

    def simple_get_mods(self, relation):
        """
        Return a simplified mod list with no subtlety nor finesse
        """
        l = []
        for dep in self.depmap:
            #Filter required only, then select first provider
            filtered_deplist = self._filter(self.depmap[dep], relation)
            if filtered_deplist:
                l.append( filtered_deplist[0][0] ) 
        return l
    
    def _filter(self, deplist, relation):
        r = []
        for dep in deplist:
            if dep[2] == relation:
                r.append(dep)
        return r
        
class ModInstance(object):
    def __init__(self, mod_id, instance=None):
        if instance:
            self.mod = instance
        else:
            self.mod = ModEntry.get(id=mod_id)

    def __unicode__(self):
        return self.mod.name
    
    def check_file(self, path, approve_if_no_dbhash=False):
        """
        Check file against hash in DB, return True or False
        If hash fails it will return False unless approve_if_no_dbhash is set to True
        """
        if self.mod.filehash:
            h = create_filehash(path)
            return h == self.mod.filehash
        return approve_if_no_dbhash
    
    def get_url(self):
        """
        Return download url for mod archive
        """
        return self.mod.service.get_mirror() + self.mod.filename
    
    def get_dependency_info(self, relation=0):
        """
        Return list of dependency keywords
        """
        return [x.dependency for x in ModDependency.select().where(ModDependency.mod==self.mod, ModDependency.relation == relation)]
    
    def resolve_dependencies(self, relation = 0, depsObject = None):
        """
        Recursively resolve dependencies
        """
        
        if not depsObject:
            depsObject = ModDependencies()
        
        deps = self.get_dependency_info(relation)
        
        for dep in deps:
            # FIXME this will fail if two mods can resolve the same dependency
            # Ideally the user should be notified and make a choice
            entries = ModDependency.select().where(ModDependency.relation == 1, ModDependency.dependency == dep)
            
            if not entries.count():
                L.warn("Could not resolve dependency for %s", dep)
            
            for entry in entries:
                m = entry.mod
                
                i = ModInstance(m.id, m)
                
                depsObject.add_dependency(dep, i, relation, self)
                
                i.resolve_dependencies(depsObject=depsObject)
            
        return depsObject
    
    def get_dependency_mods(self):
        """
        Return list of mods that are required for this mod to work
        """
        return self.resolve_dependencies().simple_get_mods(0)

def get_json(url):
    # FIXME: Gzip? Etag?
    d = urllib.urlopen(url)
    data = json.load(d)
    return data

class ModDb(object):
    def add_service(self, json_url):
        data = get_json(json_url)
        service = ModService(url=json_url)
        service.name = data["service"]["name"]
        service.set_mirrors(data["service"]["filelocations"])
        service.save()
        L.info("Service %s added", service.name)
        return service
    
    def get_module(self, modulename):
        e = ModEntry.get(name=modulename)
        return ModInstance(e.id, e)    
    
    def get_services(self):
        return ModService.select()
    
    def update_services(self):
        with db.transaction():
            for service in self.get_services():
                updater = ServiceUpdater(service)
                updater.update()
                L.info("%s: %s new, %s updated", service.name, updater.new, updater.updated)
    
    def search(self, text):
        d = ModEntry.select().where(
             (ModEntry.description.contains(text)) |
             (ModEntry.name.contains(text))
            )
        return [ModInstance(x.id, x) for x in d]
    
class ServiceUpdater(object):
    new = 0
    updated = 0
    
    # PeeWee related class functions

    def update_service_data(self, data):
        """Update the service info from JSON data"""
        self.service.name = data["service"]["name"]
        self.service.set_mirrors(data["service"]["filelocations"])
        self.service.save()

    def set_dependency_relation(self, modentry, dependencies, relation):
        """Remove old dependency data and set new for given relation
        
        Relations:
            (0, "Requires"),
            (1, "Provides"),
            (2, "Conflicts"),
            (3, "Recommends"),
        """
        ModDependency.delete().where(ModDependency.mod == modentry, ModDependency.relation == relation).execute()
        for dep in dependencies:
            ModDependency.create(mod = modentry, relation = relation, dependency = dep)

    def save_mod_instance(self, mod):
        """Create or update a mod instance in DB"""
        modentry, created = ModEntry.get_or_create(name=mod["name"], service=self.service)
            
        modentry.version = mod["version"]
        modentry.filename = mod["filename"]
        modentry.service = self.service
        
        # Optional entries
        for field in ["description", "filehash", "filesize", "homepage", "author"]:
            if mod.get(field):
                setattr(modentry, field, mod[field])
        
        modentry.save()
        
        return modentry, created
    

    # Normal class functions
    
    def __init__(self, service):
        self.service = service

    def update(self):
        data = get_json(self.service.url)
        self.update_service_data(data)
        self.update_mods(data["mods"])
        
    def update_mods(self, modlist):

        for mod in modlist:
            modentry, new_entry = self.save_mod_instance(mod)
            
            if new_entry:
                self.new += 1
            else:
                self.updated += 1
            
            self.set_dependency_relation(modentry, mod.get("depends", []), 0)                       # Requires
            self.set_dependency_relation(modentry, [modentry.name] + mod.get("provides", []), 1)    # Provides
            self.set_dependency_relation(modentry, mod.get("conflict", []), 2)                      # In conflict with
            self.set_dependency_relation(modentry, mod.get("recommends", []), 3)                     # Recommends
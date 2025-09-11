---
last-updated: 2025-09-12 01:30
cast-id: 9c9b2ca1-78c8-4169-bd09-85d36d62849b
cast-hsync:
  - cast (live)
  - nuu (live)
cast-codebases:
  - cast-monorepo
cast-version: 1
tags:
  - "#core"
type: project
base-version: 1
notion-link:
---
- [x] Better cast reporting ✅ 2025-08-28
- [x] Better conflict resolution ✅ 2025-08-28

- [ ] Documentation

- [ ] Cast sort order
```
Write a PR to implement the features such that when verifying the yaml of a casted file the cast-hsync casts are sorted by alphabetcial order 
```

- [x] (MAJOR) Rename casacading to hyperlinks in peer vaults ✅ 2025-09-11

- [ ] Fix Deletions

- [x] Naming conventions and simplifications ✅ 2025-09-11
```
Write a comprehensive PR to fix the naming conventions and simplify things inside of the cast system. 

Remove the term vault. Instead now we will just have casts.

cast-vault -> cast-hsync: cast (mode)

registering casts etc will no longer have a Cast location and a Vault location. All casts will look the same from the root, which is the Root will have Cast folder that contains all the markdown files. 

Root
	.cast
    Cast
    otherfolders
    otherfiles

So now when i do cast list it should just point to the root folder, no distinction between cast and vault and again, no such thing as vault anymore. 

Make sure all logic and behaviour remain the same. And cascade these changes across the codebase, fixing feature logic as you see fit. 
```


```
We are going to simplify the cast cbsync logic and etc with codebase sync feature. 

Every codebase will now register to one cast. Then, every single file created in the docs/cast folder will be added to the cast system and a new file will have cast-id, cast-hsync (that cast), cast-codebase (that codebase) (and other standard cast things) and then synced when cast cbsync is run in the codebase. 

Inside the cast, the field can have many entires in the cast-codebases (sometimes document is relevant in multiple). However, you can only run cast cbsync [codebase] on one specific codebase at a time. Same with the cast-hsync. A codebase can only have one cast and generated it will only point to that cast. But it should not stop the file having some hsyncs (which are not processed in the codebase module but processed seperately and elsewhere with cast hsync).

Workflow

Inside codebase: agent creates a file in docs/cast and runs cast cbsync. -> file synced. 
Inside cast: human creates a new file and addes the yaml filed cast-codebases: - codebase and then runs cast cbsync codebase. 

This process should also generate reports like cast hsync, and should also have merge conflicts like hsync, and should function very similarly etc. 

```

- [x] (Feature) Cast CLI . Interactive CLI MVP ✅ 2025-09-11
- [x] (Feature) Cast Sync . Codebase Sync MVP ✅ 2025-09-11
```
Implement a huge new feature. 

Cast Codebase (probably a seperate moduel but pretty similar to, up to you to decide)

Cast Codebase allows for parts of the docs of a codebase to be synced into the cast and viseversa. These include conceptual descriptions of the codebase, meetings about the codebase, requiremetns planning sprints etc. 

Root/
----/Cast
--------/docs.md

Codebase/
----/docs
--------/cast
------------/docs.md

Just like how you can install and register casts, now you will be able to install and register codebases. 

Codebases will be just simple nonspace seperated words like `nuu-surf-frontend` and will be now listed inside of the new yaml field `cast-codebases`. 

It's very similar to cast-hsync, however they key difference is that codebase is a differnet enity and not every codebase will be registered (though you can scan a vault to see what codebases are included).

Having a cast-codebase will register that file as part of the cast, and a cast-codebase is synced using cast cbsync [codebase] (does no run hsync!). However, when fixing the yaml and generating id, a cast-codebase has to also have one cast-hsync field (which is the origin). For example, if you create a file inside of the `nuu` cast, then added a cast-codebases: nuu-core yaml, executing cast cbsync nuu-core will scan and pick up that file, create a cast-hsync: nuu (live) entry. 

You fill in the gaps of the implementation and this idea. 

On the codebase end, we will have agents that constantly write and update files inside of docs/cast and have a protocol to do so accordingly.
```


# =============

- 

# Dependencies

- 

# End
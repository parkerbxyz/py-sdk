
import re
import os
import csv
import sys
import time
import hashlib
import zipfile
import requests
import webbrowser

from bs4 import BeautifulSoup

if sys.version_info.major >= 3:
  from urllib.parse import urljoin
else:
  from urlparse import urljoin

from guru.util import clear_dir, write_file, copy_file, download_file, to_yaml

# node types
NONE = "NONE"
BOARD_GROUP = "BOARD_GROUP"
BOARD = "BOARD"
SECTION = "SECTION"
CARD = "CARD"

def slugify(text):
  return re.sub(r"[^a-zA-Z0-9_\-]", "", text.replace(" ", "_"))

def _url_to_id(url, include_extension=True):
  id = hashlib.md5(url.encode("utf-8")).hexdigest()

  # take everything after the last . before the ?
  if include_extension:
    url = url.split("?")[0]
    extension = url.split(".")[-1]
    if len(extension) < 5:
      return "%s.%s" % (id, extension)
  return id

def _id_to_filename(id):
  return id.replace("/", "_")

def _is_local(url_or_path):
  if url_or_path.startswith("http") or url_or_path.startswith("mailto:"):
    return False
  elif url_or_path.startswith("//"):
    return False
  else:
    return True

def _parse_style(text):
  result = {}
  pairs = text.split(";")

  for pair in pairs:
    # split a "width: 400px" kind of string into the key and value.
    index = pair.find(":")
    key = pair[0:index].strip()
    value = pair[index + 1:].strip()
    result[key] = value
  
  return result

def _format_style(values):
  return ";".join(["%s:%s" % (key, values[key]) for key in values.keys()])

def clean_up_html(html):
  doc = BeautifulSoup(html, "html.parser")

  # only keep the attributes we need otherwise they just take up space.
  attributes_to_keep = [
    "style",
    "start",   # for numbered lists
    "href",    # for links...
    "target",
    "rel",
    "title",
    "src",     # for images...
    "alt",
    "height",
    "width"
  ]
  for el in doc.select("*"):
    for attr in list(el.attrs.keys()):
      if attr not in attributes_to_keep:
        del el.attrs[attr]
  
  # clean up lists inside table cells.
  for li in doc.select("td li"):
    # todo: only add the br tag if the list has a previous sibling.
    # todo: make this work for numbered lists.
    br = doc.new_tag("br")
    li.insert(0, "- ")
    li.insert(0, br)
    li.unwrap()
  
  for ul in doc.select("td ul, td ol"):
    ul.unwrap()

  for el in doc.select("colgroup, table caption, script, style"):
    el.decompose()

  # remove unnecessary things from style attributes (e.g. width/height on table cells).
  style_attrs_to_keep = [
    "background",
    "background-color",
    "color",
    "font-style",
    "font-weight",
    "text-decoration"
  ]

  for el in doc.select("[style]"):
    values = _parse_style(el.attrs["style"])
    for attr in list(values.keys()):
      if attr not in style_attrs_to_keep:
        del values[attr]
    el.attrs["style"] = _format_style(values)

    # if removing some properties left the style attribute empty, remove it altogether.
    if not el.attrs["style"].strip():
      del el.attrs["style"]

  # remove spans that have no style attributes.
  for el in doc.select("span"):
    if not el.attrs or not el.attrs.get("style"):
      el.unwrap()
  
  return str(doc).replace("\\n", "\n").replace("\\'", "'")

def traverse_tree(sync, func, node=None, parent=None, depth=0, post=False, **kwargs):
  """Does a tree traversal on the nodes and calls the provided callback (func) on each node."""
  if node:
    func(node, parent, depth, **kwargs)
    for id in node.children[0:]:
      child = sync.node(id)
      if child:
        traverse_tree(sync, func, child, node, depth + 1, post, **kwargs)
    if post:
      func(node, parent, depth, post=True, **kwargs)
  else:
    # traverse the subtree for every node that doesn't have a parent.
    for node in sync.nodes:
      if not node.parents:
        traverse_tree(sync, func, node, post=post, **kwargs)

def make_html_tree(node, parent, depth, html_pieces):
  """This builds the board/card tree in the HTML preview page."""
  indent = "&nbsp;&nbsp;" * min(3, depth)
  if node.type == CARD:
    url = node.sync.CARD_HTML_PATH % (node.sync.id, node.id)
    html_pieces.append(
      '<a href="%s" target="iframe">%s%s (%s)</a>' % (url, indent, node.title, node.type)
    )
  else:
    html_pieces.append(
      '<div>%s%s (%s)</div>' % (indent, node.title, node.type)
    )

def print_node(node, parent, depth):
  indent = "  " * min(3, depth)
  parent_str = ", parent=%s" % parent.id if parent else ""
  if node.url:
    print("%s- %s (%s, url=%s)" % (indent, node.title or node.id, node.type, node.url))
  else:
    print("%s- %s (%s)" % (indent, node.title or node.id, node.type))

def print_type(node, parent, depth):
  print("%s- %s" % ("  " * min(3, depth), node.type))

def assign_types(node, parent, depth, post=False, favor_boards=None, favor_sections=None):
  """
  When you're done adding content to a sync we call this for every
  node to figure out which nodes become board groups, boards, cards,
  or sections.

  There are two ways we can do this: favoring boards or favoring sections.
  This determines what we do if we have 3 levels of content -- if we favor
  boards, it'll be: board group > board > card. If we favor sections it'll
  be: board > section > card.
  """
  if favor_sections:
    if post:
      # post-traversal if this node is a board and it has a board as a child,
      # that means the node should actually be a board group.
      if node.type == BOARD:
        for id in node.children:
          child = node.sync.node(id)
          if child.type == BOARD:
            node.type = BOARD_GROUP
            break
    else:
      if not node.children and not parent and node.content:
        node.type = CARD
      elif node.children and depth == 0:
        node.type = BOARD
      elif not node.children:
        node.type = CARD
      elif depth > 2:
        node.type = CARD
      elif parent.type == BOARD and depth == 1:
        node.type = SECTION
      elif parent.type == SECTION and depth == 2:
        parent.type = BOARD
        node.type = SECTION
      else:
        node.type = CARD
  else:
    if not post:
      # figure out which nodes are board groups, boards, etc.
      if not node.children and not parent and node.content:
        node.type = CARD
      elif depth == 0:
        node.type = BOARD
      elif not node.children:
        node.type = CARD
      elif depth > 2:
        node.type = CARD
      elif parent.type == BOARD and depth == 1:
        node.type = BOARD
        parent.type = BOARD_GROUP
      elif parent.type == BOARD and depth == 2:
        node.type = SECTION
      elif parent.type == BOARD_GROUP:
        node.type = BOARD

def insert_nodes(node, parent, depth):
  """
  If a node that ends up being a board or board group also has
  content of its own, we need to insert additional nodes so its
  content has a place to go.

  For a board group that has content we insert a board then add
  a card to it. For a board or section that has content we just
  add a card as a child of that node.
  """
  # add content nodes/boards as needed.
  # if a board has content, make a sectionless "content" card as the first child.
  # if a board group has content, make a content board as the first child.
  
  sync = node.sync

  # board groups that have content require two new nodes -- one for the
  # card and one to be the board that contains that card.
  if node.content and node.type == BOARD_GROUP:
    # insert a board and add a card to it.
    board_id = "%s_content_board" % node.id
    content_id = "%s_content" % node.id
    board_node = sync.node(
      id=board_id,
      url=node.url,
      title="%s Content" % node.title,
      type=BOARD
    )
    node.add_child(board_node, first=True)

    content_node = sync.node(
      id=content_id,
      url=node.url,
      title=node.title,
      content=node.content,
      type=CARD
    )
    sync.node(board_id).add_child(content_node)

  # if the node has content and is a board or section we just make
  # a new node (as the card) inside this node.
  elif node.content and (node.type == BOARD or node.type == SECTION):
    # add a card as the first item inside this node.
    content_id = "%s_content" % node.id
    content_node = sync.node(
      id=content_id,
      url=node.url,
      title=node.title,
      content=node.content,
      type=CARD
    )
    node.add_child(content_node, first=True)
  
  # todo: figure out how this happens.
  # if a board group contains a card directly we need to move the card into a board.
  elif node.type == CARD and parent and parent.type == BOARD_GROUP:
    # add the card to the board group's "_content" board.
    content_id = "%s_content_board" % node.id
    content_title = "%s Content" % node.title
    content_board = sync.node(content_id, title=content_title, type=BOARD)
    node.move_to(content_board)
    parent.add_child(content_board, first=True)


class SyncNode:
  def __init__(self, id, sync, url="", title="", desc="", content="", tags=None):
    self.id = id
    self.sync = sync

    self.url = ""
    self.desc = desc
    self.title = title
    self.content = content
    self.children = []
    self.parents = []
    self.type = NONE
    self.tags = tags
  
  def add_to(self, node):
    """Adds this object as a child of the given node."""
    node.add_child(self)
    return self
  
  def detach(self):
    """Removes a node from all of its parents."""
    # for node in self.sync.nodes:
    #   if self.id in node.children:
    #     node.children.remove(self.id)
    for node in self.parents:
      node.children.remove(self.id)
    self.parents = []
    return self

  def move_to(self, parent):
    """Removes the node from all of its parents and assign it a new parent."""
    self.detach()
    parent.add_child(self)
    return self

  def ancestors(self):
    result = self.parents[:]
    index = 0
    while index < len(result):
      result += result[index].parents
      index += 1
    return result

  def add_child(self, child, first=False):
    """
    Adds the given node as a child of this one.
    
    By default children are added to the end of the parent's list
    of children but passing first=True makes the new child go first.
    """
    
    # check if 'child' is already an ancestor of 'self'.
    for ancestor in self.ancestors():
      if ancestor.id == child.id:
        raise RuntimeError("adding '%s' as a child of '%s' would create a cycle" % (
          child.title or child.id, self.title or self.id
        ))

    # nodes can only have a child once.
    if child.id in self.children:
      return
    
    child.parents.append(self)
    if first:
      self.children.insert(0, child.id)
    else:
      self.children.append(child.id)
    
    return self
  
  def _make_items_list(self):
    """This is used internally when we're building the .yaml files."""
    items = []
    for id in self.children:
      node = self.sync.node(id)
      if node.type == CARD:
        items.append({
          "ID": node.id,
          "Type": "card"
        })
        # if this node has nested children this'll flatten them out.
        items += node._make_items_list()
      elif node.type == SECTION:
        items.append({
          "Type": "section",
          "Title": node.title,
          "Items": node._make_items_list()
        })
      elif node.type == BOARD:
        items.append(node.id)
    
    return items

  def html_cleanup(self, download_func=None, convert_links=True, compare_links=None):
    """
    This adjusts image and link URLs to either be absolute or refer to
    something in this import -- for cards this means we look for href
    values that should become card-to-card links and for images we look
    for references to files in the resources folder.

    This will eventually have the ability to download images.
    """
    # we only need to clean up the html for cards that have content.
    if not self.content or self.type != CARD:
      return

    doc = BeautifulSoup(self.content, "html.parser")
    updated = False
    url_map = {}

    # this function can work on image and link URLs.
    def check_element(element, attr):
      url = element.attrs.get(attr, "")

      if url.startswith("data:") or url.startswith("mailto:"):
        return

      # remember this value so we can later check if it changed.
      initial_value = element.attrs[attr]

      # if we've already seen this URL, update this element in the same way.
      # this way if they have two links to the same file we only download it once.
      if initial_value in url_map:
        element.attrs[attr] = url_map[initial_value]
        return True

      absolute_url = urljoin(self.url, url)
      resource_id = _url_to_id(absolute_url)

      # download_func is responsible for deciding if we need to download the file
      # and for doing the download too (since you probably need auth headers for
      # the download to work).
      if download_func:

        # if we've already downloaded this file, update the src/href.
        if resource_id in self.sync.resources:
          element.attrs[attr] = self.sync.resources[resource_id]
        else:
          filename = self.sync.RESOURCE_PATH % (self.sync.id, resource_id)
          self.sync.log(message="checking if we should download attachment", url=absolute_url, file=filename)

          # returning True means the file was downloaded so we need to update the src/href.
          if download_func(absolute_url, filename):
            self.sync.log(message="download successful", url=absolute_url, file=filename)
            self.sync.resources[resource_id] = filename
            element.attrs[attr] = "resources/%s" % resource_id
          else:
            # returning False means it didn't download so we make the url absolute.
            self.sync.log(message="did not download", url=absolute_url, file=filename)
            element.attrs[attr] = absolute_url
      else:
        # if we're not downloading files we still need to do some cleanup.
        #  - move referenced attachments into the resources/ folder.
        #  - make urls absolute.

        # if it's a local html file and the src is relative,
        # add the attachment as a resource and update the url.
        if _is_local(self.url) and _is_local(url):
          # if self.url is:       /Users/rmiller/export/something.html
          # and url is:           images/bullet.gif
          # then absolute_url is: /Users/rmiller/export/images/bullet.gif
          # and filename is:      /tmp/{job_id}/resources/{hash}.gif
          filename = self.sync.RESOURCE_PATH % (self.sync.id, resource_id)
          copy_file(absolute_url, filename)
          self.sync.resources[resource_id] = filename
          element.attrs[attr] = "resources/%s" % resource_id
        elif _is_local(url):
          # this means self.url is _not_ local but the url is, so make it absolute.
          element.attrs[attr] = absolute_url
        # add protocols to image urls that are lacking them.
        # i'm pretty sure this is required but i forget why.
        elif url.startswith("//"):
          element.attrs[attr] = "https:" + url
  
      # we want to return True if the value changed.
      if element.attrs[attr] != initial_value:
        url_map[initial_value] = element.attrs[attr]
        return True
    
    # images and iframes can both have src attributes that might reference files we need
    # to download or we may need to adjust ther urls (e.g. make them absolute).
    for el in doc.select("[src]"):
      if check_element(el, "src"):
        updated = True
    
    # look for links to files that need to be downloaded.
    # also convert doc-to-doc links to be card-to-card.
    for link in doc.select("a[href]"):
      href = link.attrs.get("href", "")
      if not href:
        continue

      check_as_attachment = True
      absolute_url = urljoin(self.url, href)

      # if convert_links:
      for other_node in self.sync.nodes:
        if (compare_links and compare_links(other_node.url, absolute_url)) or \
            other_node.url == absolute_url:
          # print("replace link: %s  -->  cards/%s" % (href[0:80], other_node.id))
          link.attrs["href"] = "cards/%s" % other_node.id
          updated = True
          check_as_attachment = False
          break
      
      # find links to local files and add these files as resources.
      if check_as_attachment:
        if check_element(link, "href"):
          updated = True
    
    if updated:
      self.content = str(doc)

  def write_files(self):
    """
    Writes the files needed for this object. For cards that's a .yaml
    and .html file. For boards and board groups it's just a .yaml file.
    """
    if self.type == CARD:
      write_file(self.sync.CARD_YAML_PATH % (self.sync.id, _id_to_filename(self.id)), self.make_yaml())
      write_file(self.sync.CARD_HTML_PATH % (self.sync.id, _id_to_filename(self.id)), self.content or "")
    elif self.type == BOARD:
      write_file(self.sync.BOARD_YAML_PATH % (self.sync.id, _id_to_filename(self.id)), self.make_yaml())
    elif self.type == BOARD_GROUP:
      write_file(self.sync.BOARD_GROUP_YAML_PATH % (self.sync.id, _id_to_filename(self.id)), self.make_yaml())

  def make_yaml(self):
    """Generates the yaml content for this node."""
    if self.type == CARD:
      data = {
        "Title": self.title,
        "ExternalId": self.id
      }
      if self.url:
        data["ExternalUrl"] = self.url
      if self.tags:
        data["Tags"] = self.tags
      
      return to_yaml(data)
    elif self.type == BOARD or self.type == BOARD_GROUP:
      items_key = "Items" if self.type == BOARD else "Boards"
      data = {
        "Title": self.title,
        "ExternalId": self.id,
        items_key: self._make_items_list()
      }
      if self.url:
        data["ExternalUrl"] = self.url
      if self.desc:
        data["Description"] = self.desc
      
      return to_yaml(data)


class Sync:
  def __init__(self, guru, id="", clear=False, folder="/tmp/", verbose=False):
    self.guru = guru
    self.id = slugify(id) if id else str(int(time.time()))
    self.nodes = []
    self.resources = {}
    self.verbose = verbose
    self.events = []
    self.start_time = time.time()

    self.CONTENT_PATH = folder + "%s"
    self.ZIP_PATH = folder + "collection_%s.zip"
    self.CARD_PREVIEW_PATH = folder + "%s/index.html"
    self.CSV_PATH = folder + "%s/log.csv"
    self.CARD_YAML_PATH = folder + "%s/cards/%s.yaml"
    self.CARD_HTML_PATH = folder + "%s/cards/%s.html"
    self.BOARD_YAML_PATH = folder + "%s/boards/%s.yaml"
    self.BOARD_GROUP_YAML_PATH = folder + "%s/board-groups/%s.yaml"
    self.COLLECTION_YAML_PATH = folder + "%s/collection.yaml"
    self.RESOURCE_PATH = folder + "%s/resources/%s"

    if clear:
      clear_dir(self.CONTENT_PATH % self.id)
  
  def log(self, **kwargs):
    kwargs["time"] = time.time() - self.start_time
    self.events.append(kwargs)
    if self.verbose:
      print(kwargs)

  def _write_csv(self):
    labels = []
    for event in self.events:
      for key in event:
        if key not in labels:
          labels.append(key)

    with open(self.CSV_PATH % self.id, "w") as file_out:
      csv_out = csv.writer(file_out)
      csv_out.writerow(labels)
      for event in self.events:
        row = []
        for key in labels:
          if key in event:
            row.append(event[key])
          else:
            row.append("")
        csv_out.writerow(row)

  def has_node(self, id):
    for n in self.nodes:
      if n.id == id:
        return True
    return False
  
  def node(self, id="", url="", title="", content="", desc="", tags=None, type=None, clean_html=True):
    """
    This method makes a node or updates one. Nodes may have content but some
    may just have titles -- nodes with just titles can be used to group the
    nodes that do contain content.

    Nodes need either an ID or a URL. If you're loading data from your database
    or from an API, you might easily have IDs for each node. If you're scraping
    pages from a website you may not have IDs but you can use the URL -- we'll
    hash the URL and use that as the ID.

    Based on how you load and process data you may identify a node before you
    have all of its info. Say you load a page and it has links to its children,
    you'll know the URL and title of the children before you know what their
    content is. You can call sync.node() to create them and establish the
    parent/child relationship, then call sync.node() again later to set the
    child node's content.
    """
    id = str(id)
    if url and not id:
      id = _url_to_id(url, False)
    
    node = None
    for n in self.nodes:
      if n.id == id:
        node = n
        break
    
    if title:
      title = str(title).strip()
      if len(title) > 200:
        title = "%s..." % title[0:197]
    
    if not node:
      node = SyncNode(id, sync=self, title=title, desc=desc, content=content, tags=tags)
      self.nodes.append(node)
    
    if url:
      node.url = url
    if title:
      node.title = title
    if content:
      if clean_html:
        node.content = clean_up_html(content)
      else:
        node.content = content
    if type:
      node.type = type
    if tags:
      node.tags = tags
    
    return node
  
  def print_tree(self, just_types=False):
    if just_types:
      traverse_tree(self, print_type)
    else:
      traverse_tree(self, print_node)

  """
  def download_resource(self, url, headers=None):
    resource_id = _url_to_id(url)
    filename = self.RESOURCE_PATH % (self.id, resource_id)
    download_file(url, filename, headers=headers)
    self.resources[resource_id] = filename
    return "resources/%s" % resource_id

  def get_resource_path(self, url):
    id = _url_to_id(url)
    return self.RESOURCE_PATH % (self.id, id)

  def add_resource(self, filename):
    filename = filename.split("?")[0]
    resource_id = _url_to_id(filename)
    resource_filename = self.RESOURCE_PATH % (self.id, resource_id)
    self.resources[resource_id] = filename
    return "resources/%s" % resource_id
  """

  def _make_collection_yaml(self):
    items = []
    tags = []
    for node in self.nodes:
      if node.type == BOARD:
        if not node.parents:
          items.append({
            "ID": node.id,
            "Type": "board",
            "Title": node.title
          })
      elif node.type == BOARD_GROUP:
        items.append({
          "ID": node.id,
          "Type": "section",
          "Title": node.title
        })
      elif node.type == CARD:
        if node.tags:
          for tag in node.tags:
            if tag not in tags:
              tags.append(tag)
    
    data = {
      "Title": "test",
      "Items": items,
      "Tags": tags
    }

    return to_yaml(data)

  def zip(self, download_func=None, convert_links=True, compare_links=None, favor_boards=None, favor_sections=None):
    """
    This wraps up the sync process. Calling this lets us know you're
    done adding content so we can do these things:

    1. Assign guru types (board, card, section, etc.) to each node.
    2. Create extra nodes as needed (e.g. to have the content that's associated with a board node)
    3. Clean up link/image URLs and download resources.
    4. Write the .html and .yaml files.
    5. Make a .zip archive with all the content.
    """
    # these are done as tree traversals so we have the parent/child
    # relationship and node depth as parameters.
    traverse_tree(self, assign_types, post=True, favor_boards=favor_boards, favor_sections=favor_sections)
    traverse_tree(self, insert_nodes)

    # these are done for all nodes, the tree structure doesn't matter.
    count = 0
    for node in self.nodes:
      count += 1
      self.log(message="post-processing node %s / %s" % (count, len(self.nodes)), node=node.id)
      node.html_cleanup(
        download_func=download_func,
        convert_links=convert_links,
        compare_links=compare_links
      )
    for node in self.nodes:
      node.write_files()
    
    # write the collection.yaml file.
    write_file(self.COLLECTION_YAML_PATH % self.id, self._make_collection_yaml())

    # make sure local files are all inside the /tmp/x/resources folder.
    for res_path in self.resources:
      # src_path is the local file (could be outside /tmp/x/resources)
      # res_path is the path in /tmp/x/resources/
      src_path = self.resources[res_path]
      content_path = self.CONTENT_PATH % self.id

      if not src_path.startswith(content_path):
        self.log(message="add local file to resources", file=src_path, resource=res_path)
        copy_file(src_path, res_path)

    # build the zip file.
    zip_path = self.ZIP_PATH % self.id
    zip_file = zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True)
    content_path = self.CONTENT_PATH % self.id
    for root, dirs, files in os.walk(content_path):
      for file in files:
        if not file.startswith("."):
          src_path = os.path.join(root, file)
          dest_path = os.path.relpath(src_path, content_path)
          self.log(message="add file to zip", file=file, zip_path=dest_path)
          zip_file.write(src_path, dest_path)

    zip_file.close()
    self._write_csv()
  
  def upload(self, is_sync=False, name="", color="", desc="", collection_id=""):
    """
    Uploads the zip file you generated to Guru.

    This can either be done as a sync or import. Syncs update the content
    of an entire collection. Imports add content to a collection.

    The `name` parameter is the name of the collection to add this to. If
    there's not a collection matching that name it'll create one and you can
    provide the color and description to use for this new collection. You
    can also pass a collection_id instead of a name if you happen to know it.
    """
    if name and not collection_id:
      # get the team's list of collections and find the one matching this name.
      collection = self.guru.get_collection(name)
      collection_id = collection.id if collection else ""
      
      # if no match is found, make the collection.
      if not collection_id:
        collection_id = self.guru.make_collection(name, desc, color, is_sync).id
      else:
        # todo: make the PUT call to make sure the name, color, and desc are set correctly.
        pass
    
    if not collection_id:
      raise BaseException("collection_id is required")
    
    return self.guru.upload_content(
      collection=collection_id,
      filename="collection_%s.zip" % self.id,
      zip_path=self.ZIP_PATH % self.id,
      is_sync=is_sync
    )
  
  def view_in_browser(self, open_browser=True):
    """
    This generates an HTML page that shows the .html files in an iframe
    so you can visualize the content structure and preview the HTML.
    """
    html_pieces = []
    traverse_tree(self, make_html_tree, html_pieces=html_pieces)
    html = """<!doctype html>
<html>
  <head>
    <style>

      body {
        display: flex;
        flex-direction: row;
        margin: 0;
        position: fixed;
        left: 0;
        right: 0;
        top: 0;
        bottom: 0;
        font-family: arial, sans-serif;
        font-size: 12px;
        background: #f7f8fa;
      }

      #tree {
        padding: 10px;
        height: 100%%;
        overflow: auto;
        box-sizing: border-box;
        padding-bottom: 30px;
      }
      #tree > * {
        display: block;
        padding: 2px;
      }
      iframe {
        flex-grow: 1;
        max-width: 734px;
        margin: 20px auto;
        box-shadow: rgba(0, 0, 0, 0.15) 0 3px 10px;
        padding: 20px 60px;
        border: 1px solid #ccc;
        border-radius: 5px;
        background: #fff;
      }

      a, a:visited {
        display: block;
        color: #44f;
        text-decoration: none;
      }
      a:hover {
        background: #eef;
      }
      a.selected {
        background: #44f;
        color: #fff;
      }

    </style>
  </head>
  <body>
    <div id="tree">%s</div>
      <iframe name="iframe" src=""></iframe>
    <script>

      var links = document.querySelectorAll("#tree a");
      var currentIndex = -1;

      links.forEach(function(link, index) {
        link.onclick = function() {
          links[currentIndex].classList.remove("selected");
          currentIndex = index;
          link.classList.add("selected");
        }
      });
      function next() {
        if (links[currentIndex]) {
          links[currentIndex].classList.remove("selected");
        }
        currentIndex = (currentIndex + 1) %% links.length;
        links[currentIndex].classList.add("selected");
        links[currentIndex].click();
      }
      function prev() {
        if (links[currentIndex]) {
          links[currentIndex].classList.remove("selected");
        }
        currentIndex = (currentIndex - 1 + links.length) %% links.length;
        links[currentIndex].classList.add("selected");
        links[currentIndex].click();
      }

      document.onkeydown = function(event) {
        if (event.keyCode == 38) {
          prev();
          event.preventDefault();
        } else if (event.keyCode == 40) {
          next();
          event.preventDefault();
        }
      };
      next();

    </script>
  </body>
</html>
""" % "".join(html_pieces)

    write_file(self.CARD_PREVIEW_PATH % self.id, html)
    if open_browser:
      webbrowser.open_new_tab("file://" + self.CARD_PREVIEW_PATH % self.id)
from datetime import datetime, timedelta
import time
import re
import os
import json
from pprint import pprint
import logging
import base64

from natsort import natsorted

from main import app
from interaction_logger import StructuredEvent

from flask import url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm.exc import NoResultFound, MultipleResultsFound
from flask_security import UserMixin, RoleMixin, login_required
from sqlalchemy import event, Integer, Date, ForeignKey, Column, Table,\
                       String, Boolean, DateTime, Text, ForeignKeyConstraint,\
                       cast, func, and_, or_, Index
from sqlalchemy.ext.declarative import declared_attr

db = SQLAlchemy(app)
Model = db.Model
relationship = db.relationship
backref = db.backref

def stringToBase64(s):
    return base64.b64encode(s)

def base64ToString(b):
    return base64.b64decode(b)

def datetime_to_string(adatetime):
    return adatetime.isoformat() + 'Z'

def string_to_datetime(astring):
    try:
        return datetime.strptime(astring, '%Y-%m-%dT%H:%M:%S.%fZ')
    except ValueError:
        return datetime.strptime(astring, '%Y-%m-%dT%H:%M:%SZ')

def ensure_dirs(path):
    try: 
        os.makedirs(path)
    except OSError as e:
        if not os.path.isdir(path):
            app.logger.warning(e.args + (path, ) )

class Base(Model):
    __abstract__  = True
    @declared_attr
    def __tablename__(cls):
        return cls.__name__.lower()
    def __repr__(self):
        return str(self)

    id =  Column(Integer(), primary_key=True)
    date_created  = Column(DateTime, default=func.current_timestamp())
    date_modified = Column(DateTime, default=func.current_timestamp(),
                                     onupdate=func.current_timestamp())

                 
class User(Base, UserMixin):
    # General user properties
    id = Column(Integer(), primary_key=True)
    first_name = Column(String(255))
    last_name = Column(String(255))
    email = Column(String(255))
    gender = Column(String(255), default='Unspecified')
    picture = Column(String(255), default='') # A url
    proof = Column(String(255), default=None)
    password = Column(String(255))
    active = Column(Boolean())
    confirmed_at = Column(DateTime())
    
    # Foreign key relationships
    settings = relationship("Settings", backref='user', lazy='dynamic')
    roles = relationship("Role", backref='user', lazy='dynamic')
    authentications = relationship("Authentication", backref='user', lazy='dynamic')
    assignments = relationship("Assignment",  backref='user', lazy='dynamic')
    
    @staticmethod
    def new_from_instructor(email, first_name='', last_name=''):
        new_user = User(first_name=first_name, last_name=last_name, 
                        email=email)
        db.session.add(new_user)
        db.session.commit()
        return new_user
        
    @staticmethod
    def find_student(email):
        return User.query.filter_by(email=email).first()
    
    def get_roles(self):
        return Role.query.filter_by(user_id=self.id).all()
        
    def get_editable_courses(self):
        return (db.session.query(Course)
                          .filter(Role.user_id == self.id,
                                  Role.course_id == Course.id,
                                  (Role.name == 'instructor') | (Role.name == 'admin'))
                          .order_by(Course.name)
                          .distinct())
        
    def get_courses(self):
        return (db.session.query(Course, Role)
                          .filter(Role.user_id == self.id,
                                  Role.course_id == Course.id)
                          .order_by(Role.name)
                          .all())
    
    def __str__(self):
        return '<User {} ({})>'.format(self.id, self.email)
        
    def name(self):
        return ' '.join((self.first_name, self.last_name))
        
    def is_admin(self):
        return 'admin' in {role.name.lower() for role in self.roles.all()}
    
    def is_instructor(self, course_id=None):
        if course_id is not None:
            return 'instructor' in {role.name.lower() for role in self.roles.all()
                                    if role.course_id == course_id}
        return 'instructor' in {role.name.lower() for role in self.roles.all()}
    
    def is_grader(self, course_id=None):
        if course_id is not None:
            role_strings = {role.name.lower() for role in self.roles.all()
                            if role.course_id == course_id}
        else:
            role_strings ={role.name.lower() for role in self.roles.all()}
        return ('instructor' in role_strings or 
                'urn:lti:sysrole:ims/lis/none' in role_strings or
                'urn:lti:role:ims/lis/teachingassistant' in role_strings)
                
    def is_student(self, course_id=None):
        if course_id is not None:
            return 'learner' in {role.name.lower() for role in self.roles.all()
                                    if role.course_id == course_id}
        return 'learner' in {role.name.lower() for role in self.roles.all()}
        
    def add_role(self, name, course_id):
        new_role = Role(name=name, user_id=self.id, course_id=course_id)
        db.session.add(new_role)
        db.session.commit()
        
    def update_roles(self, new_roles, course_id):
        old_roles = [role for role in self.roles.all() if role.course_id == course_id]
        new_role_names = set(new_role_name.lower() for new_role_name in new_roles)
        for old_role in old_roles:
            if old_role.name.lower() not in new_role_names:
                Role.query.filter(Role.id == old_role.id).delete()
        old_role_names = set(role.name.lower() for role in old_roles)
        for new_role_name in new_roles:
            if new_role_name.lower() not in old_role_names:
                new_role = Role(name=new_role_name.lower(), user_id=self.id, course_id=course_id)
                db.session.add(new_role)
        db.session.commit()
        
    @staticmethod
    def is_lti_instructor(given_roles):
        ROLES = ["urn:lti:role:ims/lis/TeachingAssistant",
                 "Instructor", "ContentDeveloper",
                 "urn:lti:role:ims/lis/Instructor",
                 "urn:lti:role:ims/lis/ContentDeveloper"]
        return any(role for role in ROLES if role in given_roles)
        
    @staticmethod
    def new_lti_user(service, lti_user_id, lti_email, lti_first_name, lti_last_name):
        new_user = User(first_name=lti_first_name, last_name=lti_last_name, email=lti_email, 
                        password="", active=True, confirmed_at=None)
        db.session.add(new_user)
        db.session.flush()
        new_authentication = Authentication(type=service, 
                                            value=lti_user_id,
                                            user_id=new_user.id)
        db.session.add(new_authentication)
        db.session.commit()
        return new_user
        
    def register_authentication(self, service, lti_user_id):
        new_authentication = Authentication(type=service, 
                                            value=lti_user_id,
                                            user_id=self.id)
        db.session.add(new_authentication)
        db.session.commit()
        return self
        
    @staticmethod
    def from_lti(service, lti_user_id, lti_email, lti_first_name, lti_last_name):
        """
        For a given service (e.g., "canvas"), and a user_id in the LTI system
        """
        lti = Authentication.query.filter_by(type=service, 
                                             value=lti_user_id).first()
        if lti is None:
            user = User.query.filter_by(email=lti_email).first()
            if user:
                user.register_authentication(service, lti_user_id)
                return user
            else:
                return User.new_lti_user(service, lti_user_id, lti_email, lti_first_name, lti_last_name)
        else:
            return lti.user
        
class Course(Base):
    name = Column(String(255))
    owner_id = Column(Integer(), ForeignKey('user.id'))
    service = Column(String(80), default="native")
    external_id = Column(String(255), default="")
    visibility = Column(String(80), default="private")
    
    def encode_json(self):
        user = User.query.get(self.owner_id)
        return {'_schema_version': 1,
                'name': self.name,
                'owner_id': self.owner_id,
                'owner_id__email': user.email if user else '',
                'service': self.service,
                'external_id': self.external_id,
                'visibility': self.visibility,
                'id': self.id,
                'date_modified': datetime_to_string(self.date_modified),
                'date_created': datetime_to_string(self.date_created)}
    @staticmethod
    def decode_json(data, **kwargs):
        if data['_schema_version'] == 1:
            data = dict(data) # shallow copy
            del data['_schema_version']
            del data['owner_id__email']
            del data['id']
            del data['date_modified']
            data['date_created'] = string_to_datetime(data['date_created'])
            for key, value in kwargs.items():
                data[key] = value
            return Course(**data)
        raise Exception("Unknown schema version: {}".format(data.get('_schema_version', "Unknown")))
    
    @staticmethod
    def import_json(data, owner_id):
        course = Course.decode_json(data['course'], owner_id=owner_id)
        db.session.add(course)
        db.session.commit()
        assignment_remap = {}
        for assignment_data in natsorted(data['assignments'], key=lambda a: a['name']):
            assignment = Assignment.decode_json(assignment_data,
                                                course_id=course.id,
                                                owner_id=owner_id)
            db.session.add(assignment)
            db.session.commit()
            assignment_remap[assignment_data['id']] = assignment.id
        group_remap = {}
        for group_data in natsorted(data['assignment_groups'], key=lambda g: g['name']):
            group = AssignmentGroup.decode_json(group_data,
                                                course_id=course.id,
                                                owner_id=owner_id)
            db.session.add(group)
            db.session.commit()
            group_remap[group_data['id']] = group.id
        sorter = lambda m: (m['assignment_group_id'], m['assignment_id'])
        for member_data in sorted(data['assignment_memberships'], key=sorter):
            assignment_id = assignment_remap[member_data['assignment_id']]
            group_id = group_remap[member_data['assignment_group_id']]
            member = AssignmentGroupMembership.decode_json(member_data,
                                                           assignment_id=assignment_id,
                                                           assignment_group_id=group_id)
            db.session.add(member)
        db.session.commit()
    
    @staticmethod
    def export(course_id):
        course = Course.query.get(course_id)
        # Get all course's assignments
        course_assignments = Assignment.by_course(course_id, False)
        # Get all course's assignment groups
        groups = course.get_assignment_groups()
        assignment_groups = [a.encode_json() for a in groups]
        
        # Get all assignment groups' memberships
        assignment_memberships = [a.encode_json()
                                  for a in AssignmentGroupMembership.by_course(course_id)]
        # Get all assignment groups' assignments
        groups_assignments = {a for g in groups
                              for a in g.get_assignments()}
        groups_assignments.update(course_assignments)
        assignments = [a.encode_json() for a in groups_assignments]
        assignments.sort(key=lambda a: a['name'])
        return {
            'course': course.encode_json(),
            'assignments': assignments,
            'assignment_groups': assignment_groups,
            'assignment_memberships': assignment_memberships
        }
    
    def __str__(self):
        return '<Course {}>'.format(self.id)
        
    @staticmethod
    def get_public():
        return Course.query.filter_by(visibility='public').all()
        
    @staticmethod    
    def remove(course_id, remove_linked=False):
        Course.query.filter_by(id=course_id).delete()
        if remove_linked:
            for m in AssignmentGroupMembership.by_course(course_id):
                db.session.delete(m)
            for a in Assignment.by_course(course_id):
                db.session.delete(a)
            for g in AssignmentGroup.by_course(course_id):
                db.session.delete(g)
        db.session.commit()
        
    def get_users(self):
        return (db.session.query(Role, User)
                          .filter(Role.course_id==self.id)
                          .filter(Role.user_id==User.id).all())
    def get_students(self):
        return [x[1] for x in (db.session.query(Role, User)
                          .filter(Role.course_id==self.id)
                          .filter(Role.user_id==User.id).distinct())]
    def get_assignments(self):
        return (db.session.query(Assignment, AssignmentGroupMembership)
                          .filter(Assignment.course_id==self.id,
                                  AssignmentGroupMembership.assignment_id == Assignment.id)
                          .all())
    def get_submitted_assignments(self):
        return (db.session.query(Assignment, AssignmentGroupMembership)
                          .join(Submission, Submission.assignment_id==Assignment.id)
                          .filter(Submission.course_id==self.id,
                                  AssignmentGroupMembership.assignment_id == Assignment.id)
                          .distinct())
    def get_submissions(self):
        return (db.session.query(Submission)
                          .filter(Submission.course_id==self.id)
                          .all())
    def get_assignment_groups(self):
        return (db.session.query(AssignmentGroup)
                          .filter(AssignmentGroup.course_id==self.id)
                          .order_by(AssignmentGroup.name)
                          .all())
        
    @staticmethod
    def get_all_groups():
        courses = Course.query.all()
        return [{'id': course.id, 
                 'name': course.name, 
                 'groups': [{'id': group.id,
                             'name': group.name}
                            for group in AssignmentGroup.by_course(course.id)]
                 } 
                for course in courses]

    @staticmethod
    def rename(course_id, name=None):
        course = Course.by_id(course_id)
        if name is not None:
            course.name = name
        db.session.commit()
        return course
    
    @staticmethod    
    def new(name, owner_id, visibility):
        if visibility == 'public':
            visibility = 'public'
        else:
            visibility = 'private'
        new_course = Course(name=name, owner_id=owner_id, visibility=visibility)
        db.session.add(new_course)
        db.session.flush()
        new_role = Role(name='instructor', user_id=owner_id, course_id=new_course.id)
        db.session.add(new_role)
        db.session.commit()
        return new_course
        
    @staticmethod
    def new_lti_course(service, external_id, name, user_id):
        new_course = Course(name=name, owner_id=user_id,
                            service=service, external_id=external_id)
        db.session.add(new_course)
        db.session.commit()
        return new_course
        
    @staticmethod
    def by_id(course_id):
        return Course.query.get(course_id)
        
    @staticmethod
    def from_lti(service, lti_context_id, name, user_id):
        lti_course = Course.query.filter_by(external_id=lti_context_id).first()
        if lti_course is None:
            return Course.new_lti_course(service=service, 
                                         external_id=lti_context_id,
                                         name=name,
                                         user_id=user_id)
        else:
            return lti_course
            
    def grading_grid(self):
        # Return a list of lists of assignments/students/submissions
        assignments = []
        submissions = []
        return assignments, submissions
    
class Role(Base, RoleMixin):
    name = Column(String(80))
    user_id = Column(Integer(), ForeignKey('user.id'))
    course_id = Column(Integer(), ForeignKey('course.id'), default=None)
    
    NAMES = ['instructor', 'admin', 'student']
    
    def update_role(self, new_role):
        pass
    
    def __str__(self):
        return '<User {} is {}>'.format(self.user_id, self.name)
    
    @staticmethod    
    def remove(role_id):
        Role.query.filter_by(id=role_id).delete()
        db.session.commit()
        
    @staticmethod
    def by_id(role_id):
        return Role.query.get(role_id)
        
class CourseInvitations(Base):
    course_id = Column(Integer(), ForeignKey('course.id'), default=None)
    email = Column(String(255))

class Authentication(Base):
    type = Column(String(80))
    value = Column(String(255))
    user_id = Column(Integer(), ForeignKey('user.id'))
    
    TYPES = ['local', 'canvas']
    
    def __str__(self):
        return '<{} is {}>'.format(self.type, self.user_id)
        
class Log(Base):
    event = Column(String(255), default="")
    action = Column(String(255), default="")
    assignment_id = Column(Integer(), ForeignKey('assignment.id'))
    user_id = Column(Integer(), ForeignKey('user.id'))
    body = Column(Text(), default="")
    timestamp = Column(String(255), default="")
    
    @staticmethod    
    def new(event, action, assignment_id, user_id, body='', timestamp=''):
        # Database logging
        log = Log(event=event, action=action, assignment_id=assignment_id, user_id=user_id,
                  body=body, timestamp=timestamp)
        db.session.add(log)
        db.session.commit()
        # Single-file logging
        student_interactions_logger = logging.getLogger('StudentInteractions')
        student_interactions_logger.info(
            StructuredEvent(user_id, assignment_id, event, action, body, timestamp)
        )
        return log
    
    def __str__(self):
        return '<Log {} for {}>'.format(self.event, self.action)
        
    @staticmethod
    def calculate_feedbacks(assignment_id, course_id):
        return (db.session.query(func.count(Log.id))
                          .filter(Log.assignment_id == assignment_id)
                          #.filter(Log.course_id == course_id)
                          .filter(Log.event == 'feedback')
                          .filter(Log.action != "Success")
                          .group_by(Log.user_id)
                          .all())
                          
    @staticmethod
    def get_logs_for_course(course_id):
        course = Course.by_id(int(course_id))
        submissions = course.get_submissions()
        assignment_ids = {sub.assignment_id for sub in submissions}
        user_ids = {sub.user_id for sub in submissions}
        return (Log.query
                          .filter(Log.assignment_id.in_(assignment_ids))
                          #.filter(Log.course_id == course_id)
                          .filter(Log.user_id.in_(user_ids))
                          .all())
        
class Settings(Base):
    mode = Column(String(80))
    connected = Column(String(80))
    user_id = Column(Integer(), ForeignKey('user.id'))
    
    def __str__(self):
        return '<{} settings ({})>'.format(self.user_id, self.id)
        
class Submission(Base):
    code = Column(Text(), default="")
    # Precision of two decimal places
    status = Column(Integer(), default=0)
    correct = Column(Boolean(), default=False)
    assignment_id = Column(Integer(), ForeignKey('assignment.id'))
    course_id = Column(Integer(), ForeignKey('course.id'))
    user_id = Column(Integer(), ForeignKey('user.id'))
    assignment_version = Column(Integer(), default=0)
    version = Column(Integer(), default=0)
    url = Column(Text(), default="")
    __table_args__ = (Index('submission_index', "assignment_id", 
                            "course_id", "user_id"), )
    
    @staticmethod
    def by_id(submission_id):
        return Submission.query.get(submission_id)
    
    @staticmethod
    def full_by_id(submission_id):
        result = (db.session.query(Submission, User, Assignment)
                          .filter(Submission.user_id == User.id)
                          .filter(Submission.assignment_id == Assignment.id)
                          .filter(Submission.id == submission_id)
                          .first())
        if result is None:
            return None, None, None
        else:
            return result
    
    @staticmethod
    def by_assignment(assignment_id, course_id):
        return (db.session.query(Submission, User, Assignment)
                          .filter(Submission.user_id == User.id)
                          .filter(Submission.assignment_id == Assignment.id)
                          .filter(Submission.assignment_id == assignment_id)
                          .filter(Submission.course_id == course_id)
                          .all())
                          
    @staticmethod
    def get_latest(assignment_id, course_id):
        return (db.session.query(func.max(Submission.date_modified))
                          .filter(Submission.course_id == course_id,
                                  Submission.assignment_id == assignment_id)
                          .group_by(Submission.user_id)
                          .order_by(func.max(Submission.date_modified).desc())
                          .count())
    
    @staticmethod
    def by_student(user_id, course_id):
        return (db.session.query(Submission, User, Assignment)
                          .filter(Submission.user_id == User.id)
                          .filter(Submission.assignment_id == Assignment.id)
                          .filter(Submission.user_id == user_id)
                          .filter(Submission.course_id == course_id)
                          .all())
                          
    def __str__(self):
        return '<Submission {} for {}>'.format(self.id, self.user_id)
        
    @staticmethod
    def load(user_id, assignment_id, course_id, submission_url=""):
        submission = Submission.query.filter_by(assignment_id=assignment_id, 
                                                course_id=course_id,
                                                user_id=user_id).first()
        if not submission:
            submission = Submission(assignment_id=assignment_id, user_id=user_id, course_id=course_id, url=submission_url)
            assignment = Assignment.by_id(assignment_id)
            if assignment.mode == 'explain':
                submission.code = json.dumps(Submission.default_explanation(''))
            else:
                submission.code = assignment.starting_code
            db.session.add(submission)
            db.session.commit()
        elif submission_url:
            submission.url = submission_url
            db.session.commit()
        return submission
        
    @staticmethod
    def default_explanation(code):
        return {
                'code': code,
                'elements': {
                    'CORGIS_USE': {'line': 0, 'present': False, 'answer': '', 'name': 'CORGIS_USE'},
                    'FOR_LOOP': {'line': 0, 'present': False, 'answer': '', 'name': 'FOR_LOOP'},
                    'DICTIONARY_ACCESS': {'line': 0, 'present': False, 'answer': '', 'name': 'DICTIONARY_ACCESS'},
                    'IMPORT_CORGIS': {'line': 0, 'present': False, 'answer': '', 'name': 'IMPORT_CORGIS'},
                    'LIST_APPEND': {'line': 0, 'present': False, 'answer': '', 'name': 'LIST_APPEND'},
                    'IMPORT_MATPLOTLIB': {'line': 0, 'present': False, 'answer': '', 'name': 'IMPORT_MATPLOTLIB'},
                    'ASSIGNMENT': {'line': 0, 'present': False, 'answer': '', 'name': 'ASSIGNMENT'},
                    'MATPLOTLIB_PLOT': {'line': 0, 'present': False, 'answer': '', 'name': 'MATPLOTLIB_PLOT'},
                    'LIST_ASSIGNMENT': {'line': 0, 'present': False, 'answer': '', 'name': 'LIST_ASSIGNMENT'},
                    'NUM_ASSIGNMENT': {'line': 0, 'present': False, 'answer': '', 'name': 'NUM_ASSIGNMENT'},
                    'IF_STATEMENT': {'line': 0, 'present': False, 'answer': '', 'name': 'IF_STATEMENT'},
                    'DICT_ASSIGNMENT': {'line': 0, 'present': False, 'answer': '', 'name': 'DICT_ASSIGNMENT'},
                    'PRINT_USE': {'line': 0, 'present': False, 'answer': '', 'name': 'PRINT_USE'}
                }
        }
        
    @staticmethod
    def save_explanation_answer(user_id, assignment_id, course_id, name, answer):
        submission = Submission.query.filter_by(user_id=user_id, 
                                                course_id=course_id,
                                                assignment_id=assignment_id).first()
        submission_destructured = json.loads(submission.code)
        elements = submission_destructured['elements']
        if name in elements:
            elements[name]['answer'] = answer
            submission.code = json.dumps(submission_destructured)
            submission.version += 1
            db.session.commit()
            submission.log_code()
            return submission_destructured
        
    
    def save_explanation_code(self, code, elements):
        try:
            submission_destructured = json.loads(self.code)
        except ValueError:
            submission_destructured = {}
        if 'code' in submission_destructured:
            submission_destructured['code'] = code.decode("utf-8")
            existing_elements = submission_destructured['elements']
            for element in existing_elements:
                existing_elements[element]['present'] = False
            for element, value in elements.items():
                existing_elements[element]['line'] = value
                existing_elements[element]['present'] = True
        else:
            submission_destructured = Submission.default_explanation(code)
        self.code = json.dumps(submission_destructured)
        self.version += 1
        db.session.commit()
        self.log_code()
        return submission_destructured
        
    ELEMENT_PRIORITY_LIST = ['FOR_LOOP', 'LIST_ASSIGNMENT', 'DICTIONARY_ACCESS', 
                         'LIST_APPEND', 'IF_STATEMENT',
                         'NUM_ASSIGNMENT',
                         'IMPORT_CORGIS', 'IMPORT_MATPLOTLIB', 
                         'MATPLOTLIB_PLOT', 'CORGIS_USE', 'DICT_ASSIGNMENT', 'PRINT_USE']
                         
    @staticmethod
    def abbreviate_element_type(element_type):
        return ''.join([l[0] for l in element_type.split("_")])
    
    def load_explanation(self, max_questions):
        if not self.code:
            self.code = json.dumps(Submission.default_explanation(''))
            db.session.commit()
        submission_destructured = json.loads(self.code)
        code = submission_destructured['code']
        # Find the first FIVE
        available_elements = []
        used_lines = set()
        e = submission_destructured['elements']
        for element in Submission.ELEMENT_PRIORITY_LIST:
            # Not present?
            if not e[element]['present']:
                continue
            # Already used that line?
            if e[element]['line'][0] in used_lines:
                continue
            # Cool, then add it
            available_elements.append(e[element])
            used_lines.add(e[element]['line'][0])
            # Stop if we have enough already
            if len(available_elements) >= max_questions:
                break
        return code, available_elements
        
    def load_poll(self):
        if not self.code:
            self.code = json.dumps([])
            db.session.commit()
        return json.loads(self.code)
        
    @staticmethod
    def save_code(user_id, assignment_id, course_id, code, assignment_version, timestamp=''):
        submission = Submission.query.filter_by(user_id=user_id, 
                                                course_id=course_id,
                                                assignment_id=assignment_id).first()
        if not submission:
            submission = Submission(assignment_id=assignment_id, 
                                    user_id=user_id,
                                    course_id=course_id,
                                    code=code,
                                    assignment_version=assignment_version)
            db.session.add(submission)
        else:
            submission.code = code
            submission.version += 1
        db.session.commit()
        submission.log_code(timestamp=timestamp)
        return submission, True
        
    @staticmethod
    def save_correct(user_id, assignment_id, course_id):
        submission = Submission.query.filter_by(user_id=user_id, 
                                                assignment_id=assignment_id,
                                                course_id=course_id).first()
        if not submission:
            submission = Submission(assignment_id=self.id, 
                                    user_id=user_id,
                                    course_id=course_id,
                                    correct=True)
            db.session.add(submission)
        else:
            submission.correct = True
        db.session.commit()
        return submission
    
    def set_status(self, new_value):
        was_changed = self.status != new_value
        self.status= new_value
        db.session.commit()
        return was_changed
        
    def get_report_blockpy(self, image=""):
        if self.correct:
            message = "Success!"
        else:
            message = "Incomplete"
    
    def get_block_image(self):
        sub_blocks_folder = os.path.join(app.config['UPLOADS_DIR'], 'submission_blocks')
        image_path = os.path.join(sub_blocks_folder, str(self.id)+'.png')
        if os.path.exists(image_path):
            return url_for('blockpy.get_submission_image', 
                           submission_id=self.id, 
                           _external=True)
        return ""
    
    def save_block_image(self, image=""):
        sub_blocks_folder = os.path.join(app.config['UPLOADS_DIR'], 'submission_blocks')
        image_path = os.path.join(sub_blocks_folder, str(self.id)+'.png')
        if image != "":
            converted_image = base64ToString(image[22:])
            with open(image_path, 'wb') as image_file:
                image_file.write(converted_image);
            return url_for('blockpy.get_submission_image', 
                           submission_id=self.id, 
                           _external=True)
        elif os.path.exists(image_path):
            try:
                os.remove(image_path)
            except Exception as e:
                app.logger.info("Could not delete because:"+str(e))
        return None
        
    def log_code(self, extension='.py', timestamp=''):
        '''
        Store the code on disk, mapped to the Assignment ID and the Student ID
        '''
        # Multiple-file logging
        log = Log.new('code', 'set', self.assignment_id, 
                      self.user_id, body=self.code, timestamp=timestamp)
        
        directory = os.path.join(app.config['BLOCKLY_LOG_DIR'],
                                 str(self.assignment_id), 
                                 str(self.user_id))

        ensure_dirs(directory)
        name = time.strftime("%Y%m%d-%H%M%S")
        file_name = os.path.join(directory, name + extension)
        
        with open(file_name, 'w') as blockly_logfile:
            blockly_logfile.write(self.code)
        '''
        # Single file logging
        student_interactions_logger = logging.getLogger('StudentInteractions')
        student_interactions_logger.info(
            StructuredEvent(self.user_id, self.assignment_id, 'code', 'set', 
                            self.code, timestamp=timestamp)
        )
        '''
        
    def get_history(self):
        '''
        Retrieve all codes from disk
        '''
        '''
        logs = (Log.query
                   .with_entities(Log.body, Log.timestamp)
                   .filter_by(assignment_id=self.assignment_id,
                              user_id=self.user_id, 
                              event='code', action='set')
                   .all())
        return [{'code': log.body, 
                 'time': datetime.fromtimestamp(int(log.timestamp))
                                 .strftime("%Y%m%d-%H%M%S")}
                for log in logs]
        '''
        directory = os.path.join(app.config['BLOCKLY_LOG_DIR'],
                                 str(self.assignment_id), 
                                 str(self.user_id))
        ensure_dirs(directory)
        all_files = []
        for file_name in os.listdir(directory):
            full_path = os.path.join(directory, file_name)
            with open(full_path, 'r') as blockly_logfile:
                body = blockly_logfile.read()
                all_files.append({
                    'code': body,
                    'time': file_name[:-2]
                })
        return all_files
    
class Assignment(Base):
    url = Column(String(255), default="")
    name = Column(String(255), default="Untitled")
    body = Column(Text(), default="")
    give_feedback = Column(Text(), default="")
    on_step = Column(Text(), default="")
    starting_code = Column(Text(), default="")
    answer = Column(Text(), default="")
    due = Column(DateTime(), default=None)
    settings = Column(Text(), default='{}')
    # Type of assignment
    # "blockpy", "maze", "explain"
    type = Column(String(10), default="blockpy")
    visibility = Column(String(10), default="visible")
    disabled = Column(String(10), default="enabled")
    # View mode within interface
    # "blocks", "text", "parsons", "upload"
    mode = Column(String(10), default="blocks")
    owner_id = Column(Integer(), ForeignKey('user.id'))
    course_id = Column(Integer(), ForeignKey('course.id'))
    version = Column(Integer(), default=0)
    position = Column(Integer(), default=0)
    
    def encode_json(self):
        user = User.query.get(self.owner_id)
        return {'_schema_version': 1,
                'url': self.url,
                'name': self.name,
                'body': self.body,
                'give_feedback': self.give_feedback,
                'on_step': self.on_step,
                'starting_code': self.starting_code,
                'answer': self.answer,
                'due': self.due,
                'settings': self.settings,
                'type': self.type,
                'visibility': self.visibility,
                'disabled': self.disabled,
                'mode': self.mode,
                'owner_id': self.owner_id,
                'course_id': self.course_id,
                'owner_id__email': user.email if user else '',
                'version': self.version,
                'position': self.position,
                'id': self.id,
                'date_modified': datetime_to_string(self.date_modified),
                'date_created': datetime_to_string(self.date_created)}
    @staticmethod
    def decode_json(data, **kwargs):
        if data['_schema_version'] == 1:
            data = dict(data) # shallow copy
            del data['_schema_version']
            del data['owner_id__email']
            del data['id']
            del data['date_modified']
            data['date_created'] = string_to_datetime(data['date_created'])
            for key, value in kwargs.items():
                data[key] = value
            return Assignment(**data)
        raise Exception("Unknown schema version: {}".format(data.get('_schema_version', "Unknown")))
    
    BUILTIN_MODULES = 'Properties,Decisions,Iteration,Calculation,Output,Values,Lists,Dictionaries,Separator,Input,Conversion'.split(',')
    @staticmethod
    def edit(assignment_id, presentation=None, name=None, 
             give_feedback=None, on_step=None, starting_code=None, 
             parsons=None, text_first=None, mode=None,
             modules=None, importable=False,
             files=None,
             disable_algorithm_errors=False, disable_timeout=False,
             type='blockpy', secret=None):
        assignment = Assignment.by_id(assignment_id)
        if name is not None:
            assignment.name = name
            assignment.version += 1
        if presentation is not None:
            assignment.body = presentation
            assignment.version += 1
        if give_feedback is not None:
            assignment.give_feedback = give_feedback
            assignment.version += 1
        if on_step is not None:
            assignment.on_step = on_step
            assignment.version += 1
        if starting_code is not None:
            assignment.starting_code = starting_code
            assignment.version += 1
        # Is this check necessary and correct? It works fine for now...
        if modules is not None:
            if not assignment.settings:
                assignment.settings = "{}"
            settings = json.loads(assignment.settings)
            if 'modules' not in settings:
                settings['modules'] = {'added': [], 'removed': []}
            if 'importable' not in settings:
                settings['importable'] = False
            if 'disable_algorithm_errors' not in settings:
                settings['disable_algorithm_errors'] = False
            if 'disable_timeout' not in settings:
                settings['disable_timeout'] = False
            kept_modules = modules.split(",")
            settings['importable'] = importable
            settings['secret'] = secret if secret is not None else settings.get('secret', False)
            settings['disable_algorithm_errors'] = disable_algorithm_errors
            settings['disable_timeout'] = disable_timeout
            settings['modules']['added'] = [m for m in kept_modules 
                                            if m not in Assignment.BUILTIN_MODULES]
            settings['modules']['removed'] = [m for m in Assignment.BUILTIN_MODULES 
                                              if m not in kept_modules and m != 'Separator']
            settings['files'] = [f for f in files.split(",") if f]
            assignment.settings = json.dumps(settings)
        assignment.type = type
        if parsons is True:
            assignment.mode = 'parsons'
            assignment.version += 1
        elif assignment.mode == "parsons":
            # I'll be honest, I don't know what's going on here.
            assignment.mode = 'parsons'
            assignment.version += 1
        elif parsons is False and assignment.type == 'blockpy':
            assignment.mode = 'blocks'
            assignment.version += 1
        if mode is not None:
            assignment.mode = mode
            assignment.version += 1
        if text_first is True:
            assignment.mode = 'text'
            assignment.version += 1
        db.session.commit()
        return assignment
    
    def to_dict(self):
        return {
            'name': self.name,
            'id': self.id,
            'body': self.body,
            'title': self.title()
        }
    
    def __str__(self):
        return '<Assignment {} for {}>'.format(self.id, self.course_id)
        
    def title(self):
        return self.name if self.name != "Untitled" else "Untitled ({})".format(self.id)
        
    @staticmethod
    def get_available():
        assignments = Assignment.query.all()
        return [(assignment, (AssignmentGroup.query
                           .filter(AssignmentGroup.id == AssignmentGroupMembership.assignment_group_id,
                                   assignment.id == AssignmentGroupMembership.assignment_id)
                           .first()))
                for assignment in assignments]
        '''
        return (db.session.query(Assignment, AssignmentGroup)
                          .outerjoin(AssignmentGroup, AssignmentGroup.course_id == Course.id)
                          .filter(Assignment.course_id == Course.id,
                                  or_(AssignmentGroup.id == None,
                                    and_(AssignmentGroupMembership.assignment_id == Assignment.id,
                                         AssignmentGroupMembership.assignment_group_id == AssignmentGroup.id))
                                  )
                          .all())'''
    
    @staticmethod
    def new(owner_id, course_id, type="blockpy", name=None, level=None):
        if name is None:
            name = 'Untitled'
        assignment = Assignment(owner_id=owner_id, course_id=course_id, 
                                type=type, name=level if type == 'maze' else name)
        db.session.add(assignment)
        db.session.commit()
        return assignment
        
    def move_course(self, new_course_id):
        self.course_id = new_course_id
        AssignmentGroupMembership.query.filter_by(assignment_id=self.id).delete()
        db.session.commit()
    
    @staticmethod
    def remove(assignment_id):
        Assignment.query.filter_by(id=assignment_id).delete()
        db.session.commit()
        
    @staticmethod
    def is_in_course(assignment_id, course_id):
        return Assignment.query.get(assignment_id).course_id == course_id
    
    @staticmethod
    def by_course(course_id, exclude_builtins=True):
        if exclude_builtins:
            return (Assignment.query.filter_by(course_id=course_id)
                                    .filter(Assignment.mode != 'maze')
                                    .all())
        else:
            return Assignment.query.filter_by(course_id=course_id).all()
    
    @staticmethod
    def by_id(assignment_id):
        return Assignment.query.get(assignment_id)
    
    @staticmethod    
    def by_builtin(type, id, owner_id, course_id):
        assignment = Assignment.query.filter_by(course_id=course_id,
                                                mode=type,
                                                name=id).first()
        if not assignment:
            assignment = Assignment.new(owner_id, course_id)
            assignment.mode = type
            assignment.name = id
            db.session.commit()
        return assignment
    @staticmethod
    def by_id_or_new(assignment_id, owner_id, course_id):
        if assignment_id is None:
            assignment = None
        else:
            assignment = Assignment.query.get(assignment_id)
        if not assignment:
            assignment = Assignment.new(owner_id, course_id)
        return assignment
    
    def context_is_valid(self, context_id):
        course = Course.query.get(self.course_id)
        if course:
            return course.external_id == context_id
        return False
    
    def get_submission(self, user_id, course_id, submission_url=""):
        return Submission.load(user_id, self.id, course_id, submission_url=submission_url)
        
class CourseAssignment(Base):
    __tablename__ = 'courseassignment'
    assignment_id = Column(Integer(), ForeignKey('assignment.id'))
    course_id = Column(Integer(), ForeignKey('course.id'))
        
class AssignmentGroup(Base):
    __tablename__ = 'assignmentgroup'
    name = Column(String(255), default="Untitled")
    owner_id = Column(Integer(), ForeignKey('user.id'))
    course_id = Column(Integer(), ForeignKey('course.id'))
    position = Column(Integer(), default=0)
    
    def __str__(self):
        return 'Group {} in {}'.format(self.name, self.course_id)
    
    def encode_json(self):
        user = User.query.get(self.owner_id)
        return {'_schema_version': 1,
                'name': self.name,
                'owner_id': self.owner_id,
                'owner_id__email': user.email if user else '',
                'course_id': self.course_id,
                'position': self.position,
                'id': self.id,
                'date_modified': datetime_to_string(self.date_modified),
                'date_created': datetime_to_string(self.date_created)}
    @staticmethod
    def decode_json(data, **kwargs):
        if data['_schema_version'] == 1:
            data = dict(data) # shallow copy
            del data['_schema_version']
            del data['owner_id__email']
            del data['id']
            del data['date_modified']
            data['date_created'] = string_to_datetime(data['date_created'])
            for key, value in kwargs.items():
                data[key] = value
            return AssignmentGroup(**data)
        raise Exception("Unknown schema version: {}".format(data.get('_schema_version', "Unknown")))
    
    @staticmethod    
    def new(owner_id, course_id, name="Untitled Group"):
        last = (db.session.query(func.max(AssignmentGroup.position).label("last_position"))
                               .filter_by(course_id=course_id).one()).last_position
        assignment_group = AssignmentGroup(owner_id=owner_id, course_id=course_id,
                                           name=name,
                                           position=last+1 if last else 1)
        db.session.add(assignment_group)
        db.session.commit()
        return assignment_group
        
    @staticmethod    
    def remove(assignment_group_id):
        # Reorder existing
        group = AssignmentGroup.query.filter_by(id=assignment_group_id).one()
        position = group.position
        all_groups = (AssignmentGroup.query
                                     .filter(AssignmentGroup.course_id==group.course_id,
                                             AssignmentGroup.position>position)
                                     .update({"position": (AssignmentGroup.position-1)}))
        # Delete target
        AssignmentGroup.query.filter_by(id=assignment_group_id).delete()
        AssignmentGroupMembership.query.filter_by(assignment_group_id=assignment_group_id).delete()
        db.session.commit()
        
    @staticmethod
    def edit(assignment_group_id, name=None, move=0):
        assignment_group = AssignmentGroup.by_id(assignment_group_id)
        if name is not None:
            assignment_group.name = name
        if move == 1 or move == -1:
            adjacent_group = (AssignmentGroup.query.filter_by(course_id=assignment_group.course_id,
                                                          position=assignment_group.position+move).first())
            if adjacent_group:
                adjacent_group.position -= move
                assignment_group.position += move
        db.session.commit()
        return assignment_group
        
    @staticmethod
    def is_in_course(assignment_group_id, course_id):
        return AssignmentGroup.query.get(assignment_group_id).course_id == course_id
    
    @staticmethod
    def by_id(assignment_group_id):
        return AssignmentGroup.query.get(assignment_group_id)
        
    @staticmethod
    def by_course(course_id):
        return (AssignmentGroup.query.filter_by(course_id=course_id)
                                     .order_by(AssignmentGroup.name)
                                     .all())
    
    @staticmethod
    def get_ungrouped_assignments(course_id):
        return (Assignment.query
                          .filter_by(course_id=course_id)
                          .outerjoin(AssignmentGroupMembership)
                          .filter(AssignmentGroupMembership.assignment_id==None)
                          .all())
    
    def get_assignments(self):
        return (Assignment.query
                          .join(AssignmentGroupMembership, 
                                AssignmentGroupMembership.assignment_id == Assignment.id)
                          .filter(AssignmentGroupMembership.assignment_group_id==self.id)
                          .order_by(Assignment.name, AssignmentGroupMembership.position)
                          .all())
        
class AssignmentGroupMembership(Base):
    __tablename__ = 'assignmentgroupmembership'
    assignment_group_id = Column(Integer(), ForeignKey('assignmentgroup.id'))
    assignment_id = Column(Integer(), ForeignKey('assignment.id'))
    position = Column(Integer())
    
    def __str__(self):
        return "<Membership {} in {}>".format(self.assignment_id, self.assignment_group_id)
    
    def encode_json(self):
        return {'_schema_version': 1,
                'assignment_group_id': self.assignment_group_id,
                'assignment_id': self.assignment_id,
                'position': self.position,
                'id': self.id,
                'date_modified': datetime_to_string(self.date_modified),
                'date_created': datetime_to_string(self.date_created)}

    @staticmethod
    def by_course(course_id):
        groups = [g.id for g in AssignmentGroup.by_course(course_id)]
        return (AssignmentGroupMembership
                    .query
                    .filter(AssignmentGroupMembership.assignment_group_id.in_(groups))
                    .order_by(AssignmentGroupMembership.assignment_group_id,
                              AssignmentGroupMembership.assignment_id)
                    .all())
    
    @staticmethod
    def decode_json(data, **kwargs):
        if data['_schema_version'] == 1:
            data = dict(data) # shallow copy
            del data['_schema_version']
            del data['id']
            del data['date_modified']
            data['date_created'] = string_to_datetime(data['date_created'])
            for key, value in kwargs.items():
                data[key] = value
            return AssignmentGroupMembership(**data)
        raise Exception("Unknown schema version: {}".format(data.get('_schema_version', "Unknown")))
    
    @staticmethod
    def move_assignment(assignment_id, new_group_id):
        membership = (AssignmentGroupMembership.query
                                               .filter_by(assignment_id=assignment_id)
                                               .first())
        if membership is None and new_group_id != -1:
            # -1 means delete
            membership = AssignmentGroupMembership(assignment_group_id=new_group_id,
                                                   assignment_id=assignment_id,
                                                   position=0)
            db.session.add(membership)
        elif new_group_id == -1:
            db.session.delete(membership)
        else:
            membership.assignment_group_id = new_group_id
        db.session.commit()
        return membership

'''        
class Question(Base):
    assignment_id = Column(Integer(), ForeignKey('assignment.id'))
    body = Column(Text(), default="")
'''

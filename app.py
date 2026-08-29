import io
import os
import json
from datetime import datetime
from urllib.parse import quote_plus

from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename

from config.job_roles import JOB_ROLES
from utils.resume_parser import ResumeParser
from utils.resume_analyzer import ResumeAnalyzer

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'change-this-secret-key')
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024


app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///resume_analyzer.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
parser = ResumeParser()
analyzer = ResumeAnalyzer()


ALLOWED_EXTENSIONS = {'pdf', 'docx'}


class Resume(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    raw_text = db.Column(db.Text, nullable=False)
    target_role = db.Column(db.String(150), nullable=False)
    target_category = db.Column(db.String(150), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    analysis = db.relationship('ResumeAnalysis', backref='resume', uselist=False, cascade='all, delete-orphan')


class ResumeAnalysis(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    resume_id = db.Column(db.Integer, db.ForeignKey('resume.id'), nullable=False, unique=True)
    ats_score = db.Column(db.Float, default=0)
    keyword_match_score = db.Column(db.Float, default=0)
    section_score = db.Column(db.Float, default=0)
    format_score = db.Column(db.Float, default=0)
    analysis_json = db.Column(db.Text, nullable=False, default='{}')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def data(self):
        return json.loads(self.analysis_json or '{}')


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def flatten_roles():
    result = []
    for category, roles in JOB_ROLES.items():
        for role in roles:
            result.append({'name': role, 'category': category, **roles[role]})
    return result


def role_info(role):
    for category, roles in JOB_ROLES.items():
        if role in roles:
            return category, roles[role]
    return None, None


def current_analysis():
    resume_id = session.get('current_resume_id')
    if not resume_id:
        return None
    return Resume.query.get(resume_id)


@app.context_processor
def inject_globals():
    return {'roles': flatten_roles()}


@app.route('/')
def home():
    return render_template('home.html')


@app.route('/analyzer', methods=['GET', 'POST'])
def analyzer_page():
    if request.method == 'POST':
        uploaded = request.files.get('resume')
        target_role = request.form.get('target_role', '').strip()
        if not uploaded or not uploaded.filename:
            flash('Please upload a PDF or DOCX resume.', 'error')
            return redirect(url_for('analyzer_page'))
        if not allowed_file(uploaded.filename):
            flash('Only PDF and DOCX files are supported.', 'error')
            return redirect(url_for('analyzer_page'))
        category, requirements = role_info(target_role)
        if not requirements:
            flash('Please select a valid target role.', 'error')
            return redirect(url_for('analyzer_page'))

        try:
            uploaded.stream.seek(0)
            resume_text = parser.extract_text(uploaded)
            if not resume_text.strip():
                flash('No readable text was found in this file.', 'error')
                return redirect(url_for('analyzer_page'))

            result = analyzer.analyze_resume(
                {'raw_text': resume_text},
                requirements
            )
            if result.get('document_type') != 'resume':
                flash(result.get('suggestions', ['Please upload a resume.'])[0], 'error')
                return redirect(url_for('analyzer_page'))

            resume = Resume(
                filename=secure_filename(uploaded.filename),
                raw_text=resume_text,
                target_role=target_role,
                target_category=category,
            )
            db.session.add(resume)
            db.session.flush()

            analysis = ResumeAnalysis(
                resume_id=resume.id,
                ats_score=result.get('ats_score', 0),
                keyword_match_score=result.get('keyword_match', {}).get('score', 0),
                section_score=result.get('section_score', 0),
                format_score=result.get('format_score', 0),
                analysis_json=json.dumps(result, default=str),
            )
            db.session.add(analysis)
            db.session.commit()
            session['current_resume_id'] = resume.id
            return redirect(url_for('dashboard'))
        except Exception as exc:
            db.session.rollback()
            flash(f'Error processing resume: {exc}', 'error')
            return redirect(url_for('analyzer_page'))

    return render_template('analyzer.html')


@app.route('/dashboard')
def dashboard():
    resume = current_analysis()
    if not resume or not resume.analysis:
        return redirect(url_for('analyzer_page'))
    result = resume.analysis.data()
    return render_template('dashboard.html', resume=resume, result=result)


@app.route('/history')
def history():
    # Database-backed history is separate from the current-resume dashboard.
    resumes = Resume.query.order_by(Resume.created_at.desc()).limit(20).all()
    return render_template('history.html', resumes=resumes)


@app.route('/history/<int:resume_id>')
def history_item(resume_id):
    resume = Resume.query.get_or_404(resume_id)
    session['current_resume_id'] = resume.id
    return redirect(url_for('dashboard'))


@app.route('/jobs')
def jobs():
    role = request.args.get('role', '')
    selected = role_info(role)[1] if role else None
    links = []
    if role:
        q = quote_plus(role)
        links = [
            ('LinkedIn Jobs', f'https://www.linkedin.com/jobs/search/?keywords={q}'),
            ('Indeed', f'https://www.indeed.com/jobs?q={q}'),
            ('Naukri', f'https://www.naukri.com/{q.replace("+", "-")}-jobs'),
        ]
    return render_template('jobs.html', selected_role=role, selected=selected, links=links)

        
@app.route('/export/excel')
def export_excel():
    resume = current_analysis()
    if not resume or not resume.analysis:
        flash('Analyze a resume first.', 'error')
        return redirect(url_for('analyzer_page'))
    import pandas as pd
    result = resume.analysis.data()
    df = pd.DataFrame([{
        'filename': resume.filename,
        'target_role': resume.target_role,
        'name': result.get('name', ''),
        'email': result.get('email', ''),
        'phone': result.get('phone', ''),
        'ats_score': result.get('ats_score', 0),
        'keyword_match_score': result.get('keyword_match', {}).get('score', 0),
        'section_score': result.get('section_score', 0),
        'format_score': result.get('format_score', 0),
        'matched_skills': ', '.join(result.get('keyword_match', {}).get('found_skills', [])),
        'missing_skills': ', '.join(result.get('keyword_match', {}).get('missing_skills', [])),
        'recommendations': ' | '.join(result.get('suggestions', [])),
    }])
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Resume Analysis')
    output.seek(0)
    return send_file(output, as_attachment=True, download_name='resume_analysis.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/about')
def about():
    return render_template('about.html')


with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)

pipeline {
  agent any
  stages {
    stage('Build Docker Image') {
      steps {
        sh 'echo $USER'
        sh 'docker container rm -f finetune || true'
        sh './docker/build_docker.sh '
      }
    }
    stage('Start Docker Image') {
      steps {
        sh './docker/start_docker.sh'
      }
    }
    stage('Test') {
      steps {
        sh 'docker exec finetune pytest'
      }
    }

  }
  post { 
    always { 
      steps {
        cleanWs()
        sh 'docker rm -f finetune'
      }
    }
  }
}